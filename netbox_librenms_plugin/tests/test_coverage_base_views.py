"""
Coverage tests for base view classes:
  - views/base/cables_view.py   (~30% → target 95%+)
  - views/base/interfaces_view.py (~14% → target 95%+)
  - views/base/ip_addresses_view.py (~34% → target 95%+)

All tests follow the project conventions:
  - Plain pytest classes, NO @pytest.mark.django_db
  - Mock ALL database interactions with MagicMock
  - Inline imports inside test methods
  - assert x == y style
  - No RequestFactory — mock request objects directly
"""

from unittest.mock import MagicMock, patch


# =============================================================================
# Helpers
# =============================================================================


def _mock_obj(model_name="device", pk=1, name="test-device"):
    obj = MagicMock()
    obj._meta = MagicMock()
    obj._meta.model_name = model_name
    obj.pk = pk
    obj.name = name
    return obj


def _mock_request(path="/plugins/librenms/device/1/cables/"):
    req = MagicMock()
    req.path = path
    req.GET = {}
    req.POST = {}
    req.headers = {}
    return req


# =============================================================================
# BaseCableTableView
# =============================================================================


class TestBaseCableTableViewGetLinksData:
    """Tests for BaseCableTableView.get_links_data."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view.request = _mock_request()
        view.librenms_id = 42
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_get_links_data_returns_none_on_api_error(self):
        """When API returns failure, get_links_data returns None."""
        view = self._make_view()
        view._librenms_api.get_device_links.return_value = (False, {"error": "timeout"})

        obj = _mock_obj()

        with patch.object(view, "get_ports_data", return_value={"ports": []}):
            result = view.get_links_data(obj)

        assert result is None

    def test_get_links_data_returns_none_when_error_key_present(self):
        """When response has 'error' key even with success=True, returns None."""
        view = self._make_view()
        view._librenms_api.get_device_links.return_value = (True, {"error": "some error"})

        obj = _mock_obj()

        with patch.object(view, "get_ports_data", return_value={"ports": []}):
            result = view.get_links_data(obj)

        assert result is None

    def test_get_links_data_success_returns_link_list(self):
        """Successful API call returns formatted link list."""
        view = self._make_view()

        links_data = {
            "links": [
                {
                    "local_port_id": 10,
                    "remote_port": "Gi0/1",
                    "remote_hostname": "switch-b",
                    "remote_port_id": 20,
                    "remote_device_id": 99,
                }
            ]
        }
        view._librenms_api.get_device_links.return_value = (True, links_data)

        ports = {
            "ports": [
                {"port_id": 10, "ifName": "Gi0/0"},
            ]
        }
        obj = _mock_obj()

        with (
            patch.object(view, "get_ports_data", return_value=ports),
            patch("netbox_librenms_plugin.views.base.cables_view.get_interface_name_field", return_value="ifName"),
        ):
            result = view.get_links_data(obj)

        assert result is not None
        assert len(result) == 1
        assert result[0]["local_port"] == "Gi0/0"
        assert result[0]["remote_device"] == "switch-b"

    def test_get_links_data_port_without_id_skipped(self):
        """Ports missing port_id are skipped when building local port map."""
        view = self._make_view()

        links_data = {"links": [{"local_port_id": 10, "remote_hostname": "sw", "remote_port": "Gi0/1"}]}
        view._librenms_api.get_device_links.return_value = (True, links_data)

        ports = {
            "ports": [
                {"port_id": None, "ifName": "Gi0/0"},  # No port_id — skipped
                {"port_id": 10, "ifName": "Gi0/0"},
            ]
        }
        obj = _mock_obj()

        with (
            patch.object(view, "get_ports_data", return_value=ports),
            patch("netbox_librenms_plugin.views.base.cables_view.get_interface_name_field", return_value="ifName"),
        ):
            result = view.get_links_data(obj)

        assert result is not None


class TestBaseCableTableViewGetDeviceByIdOrName:
    """Tests for BaseCableTableView.get_device_by_id_or_name."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_finds_device_by_librenms_id(self):
        """When remote_device_id matches librenms_id, device is returned."""
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"

        mock_device = MagicMock()
        sentinel_q = object()

        with (
            patch("netbox_librenms_plugin.views.base.cables_view.Device") as MockDevice,
            patch("netbox_librenms_plugin.views.base.cables_view._librenms_id_q", return_value=sentinel_q) as mock_q,
        ):
            MockDevice.objects.get.return_value = mock_device

            device, found, error = view.get_device_by_id_or_name(42, "switch.example.com")

        assert found is True
        assert device is mock_device
        assert error is None
        mock_q.assert_called_once_with("default", 42)
        MockDevice.objects.get.assert_called_once_with(sentinel_q)

    def test_falls_back_to_name_when_no_id(self):
        """When remote_device_id is None, falls back to name lookup."""
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"

        mock_device = MagicMock()

        with patch("netbox_librenms_plugin.views.base.cables_view.Device") as MockDevice:
            MockDevice.objects.get.return_value = mock_device
            device, found, error = view.get_device_by_id_or_name(None, "switch-a")

        assert found is True
        assert device is mock_device

    def test_falls_back_to_simple_hostname_when_fqdn_not_found(self):
        """When FQDN name not found, tries short hostname."""

        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"

        mock_device = MagicMock()
        call_args = []

        class _DoesNotExist(Exception):
            pass

        def get_side_effect(name=None, **kwargs):
            call_args.append(name)
            if name == "switch.example.com":
                raise _DoesNotExist("not found")
            if name == "switch":
                return mock_device
            raise Exception(f"Unexpected: {name}")

        with patch("netbox_librenms_plugin.views.base.cables_view.Device") as MockDevice:
            MockDevice.DoesNotExist = _DoesNotExist
            MockDevice.objects.get.side_effect = get_side_effect
            device, found, error = view.get_device_by_id_or_name(None, "switch.example.com")

        assert found is True
        assert device is mock_device
        assert "switch.example.com" in call_args
        assert "switch" in call_args

    def test_multiple_objects_returns_error_message(self):
        """MultipleObjectsReturned for name lookup returns error info."""
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"

        with patch("netbox_librenms_plugin.views.base.cables_view.Device") as MockDevice:
            from django.core.exceptions import MultipleObjectsReturned

            class _DoesNotExist(Exception):
                pass

            MockDevice.DoesNotExist = _DoesNotExist
            MockDevice.objects.get.side_effect = MultipleObjectsReturned("Multiple")
            device, found, error = view.get_device_by_id_or_name(None, "duplicate-switch")

        assert found is False
        assert device is None
        assert error is not None
        assert "duplicate-switch" in error

    def test_device_not_found_returns_none_false_none(self):
        """When device not found by any method, returns (None, False, None)."""
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"

        class _DoesNotExist(Exception):
            pass

        with patch("netbox_librenms_plugin.views.base.cables_view.Device") as MockDevice:
            MockDevice.DoesNotExist = _DoesNotExist
            MockDevice.objects.get.side_effect = _DoesNotExist("not found")
            device, found, error = view.get_device_by_id_or_name(None, "nonexistent")

        assert found is False
        assert device is None


class TestBaseCableTableViewEnrichLocalPort:
    """Tests for BaseCableTableView.enrich_local_port."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_no_local_port_skips_enrichment(self):
        """When local_port is absent, link dict is unchanged."""
        view = self._make_view()
        link = {"local_port": None}
        obj = _mock_obj()
        view.enrich_local_port(link, obj)
        assert "local_port_url" not in link

    def test_interface_found_by_librenms_id_adds_url(self):
        """When interface found by librenms_id, local_port_url and id are set."""
        view = self._make_view()

        iface = MagicMock()
        iface.pk = 5

        obj = _mock_obj()
        obj.virtual_chassis = None
        obj.interfaces = MagicMock()
        obj.interfaces.filter.return_value.first.return_value = iface

        link = {"local_port": "Gi0/0", "local_port_id": 10}

        with patch("netbox_librenms_plugin.views.base.cables_view.reverse", return_value="/dcim/interfaces/5/"):
            view.enrich_local_port(link, obj)

        assert link["local_port_url"] == "/dcim/interfaces/5/"
        assert link["netbox_local_interface_id"] == 5

    def test_interface_found_by_name_fallback(self):
        """When librenms_id match fails, falls back to name matching."""
        view = self._make_view()

        iface = MagicMock()
        iface.pk = 7

        obj = _mock_obj()
        obj.virtual_chassis = None
        obj.interfaces = MagicMock()
        # First filter (librenms_id) returns nothing, second (name) returns iface
        obj.interfaces.filter.return_value.first.side_effect = [None, iface]

        link = {"local_port": "Gi0/1", "local_port_id": 20}

        with patch("netbox_librenms_plugin.views.base.cables_view.reverse", return_value="/dcim/interfaces/7/"):
            view.enrich_local_port(link, obj)

        assert link.get("local_port_url") == "/dcim/interfaces/7/"

    def test_no_interface_found_leaves_link_unchanged(self):
        """When no interface found, link dict is not modified."""
        view = self._make_view()

        obj = _mock_obj()
        obj.virtual_chassis = None
        obj.interfaces = MagicMock()
        obj.interfaces.filter.return_value.first.return_value = None

        link = {"local_port": "Gi0/2", "local_port_id": 30}

        view.enrich_local_port(link, obj)

        assert "local_port_url" not in link

    def test_virtual_chassis_delegates_to_chassis_member(self):
        """With VC, chassis member interfaces are queried."""
        view = self._make_view()

        vc = MagicMock()
        member = MagicMock()
        member.interfaces = MagicMock()

        iface = MagicMock()
        iface.pk = 3
        member.interfaces.filter.return_value.first.return_value = iface

        obj = _mock_obj()
        obj.virtual_chassis = vc
        # Non-VC fallback returns nothing to confirm member path is used
        obj.interfaces = MagicMock()
        obj.interfaces.filter.return_value.first.return_value = None

        link = {"local_port": "Gi1/0/1", "local_port_id": 100}

        with (
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_virtual_chassis_member",
                return_value=member,
            ) as mock_get_vc_member,
            patch("netbox_librenms_plugin.views.base.cables_view.reverse", return_value="/dcim/interfaces/3/"),
        ):
            view.enrich_local_port(link, obj)

        mock_get_vc_member.assert_called_once_with(obj, "Gi1/0/1")
        assert link["local_port_url"] == "/dcim/interfaces/3/"


class TestBaseCableTableViewCheckCableStatus:
    """Tests for BaseCableTableView.check_cable_status."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        return view

    def test_cable_found_sets_cable_url(self):
        """Existing cable: cable_status='Cable Found' and cable_url set."""
        view = self._make_view()

        cable = MagicMock()
        cable.pk = 99

        local_iface = MagicMock()
        local_iface.cable = cable
        remote_iface = MagicMock()
        remote_iface.cable = None

        link = {"netbox_local_interface_id": 1, "netbox_remote_interface_id": 2}

        with (
            patch("netbox_librenms_plugin.views.base.cables_view.Interface") as MockIface,
            patch("netbox_librenms_plugin.views.base.cables_view.reverse", return_value="/dcim/cables/99/"),
        ):
            MockIface.objects.get.side_effect = [local_iface, remote_iface]
            result = view.check_cable_status(link)

        assert result["cable_status"] == "Cable Found"
        assert result["cable_url"] == "/dcim/cables/99/"
        assert result["can_create_cable"] is False

    def test_no_cable_sets_can_create_cable(self):
        """No cable: cable_status='No Cable' and can_create_cable=True."""
        view = self._make_view()

        local_iface = MagicMock()
        local_iface.cable = None
        remote_iface = MagicMock()
        remote_iface.cable = None

        link = {"netbox_local_interface_id": 1, "netbox_remote_interface_id": 2}

        with patch("netbox_librenms_plugin.views.base.cables_view.Interface") as MockIface:
            MockIface.objects.get.side_effect = [local_iface, remote_iface]
            result = view.check_cable_status(link)

        assert result["cable_status"] == "No Cable"
        assert result["can_create_cable"] is True

    def test_missing_local_interface_id(self):
        """Only remote interface found: status = 'Local Interface Not Found in Netbox'."""
        view = self._make_view()
        link = {"netbox_local_interface_id": None, "netbox_remote_interface_id": 2}

        result = view.check_cable_status(link)

        assert "Local Interface Not Found" in result["cable_status"]
        assert result["can_create_cable"] is False

    def test_missing_remote_interface_id(self):
        """Only local interface found: status = 'Remote Interface Not Found in Netbox'."""
        view = self._make_view()
        link = {"netbox_local_interface_id": 1, "netbox_remote_interface_id": None}

        result = view.check_cable_status(link)

        assert "Remote Interface Not Found" in result["cable_status"]

    def test_both_interfaces_missing(self):
        """Both interfaces missing: status = 'Both Interfaces Not Found in Netbox'."""
        view = self._make_view()
        link = {"netbox_local_interface_id": None, "netbox_remote_interface_id": None}

        result = view.check_cable_status(link)

        assert "Both Interfaces Not Found" in result["cable_status"]


class TestBaseCableTableViewEnrichLinksData:
    """Tests for BaseCableTableView.enrich_links_data."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_enrich_links_data_calls_enrich_per_link(self):
        """enrich_links_data calls enrich_local_port and process_remote_device per link."""
        view = self._make_view()

        link1 = {"local_port": "Gi0/0", "local_port_id": 1, "remote_device": "sw-b"}
        link2 = {"local_port": "Gi0/1", "local_port_id": 2, "remote_device": None}
        obj = _mock_obj()

        with (
            patch.object(view, "enrich_local_port") as mock_enrich_local,
            patch.object(view, "process_remote_device", return_value=link1) as mock_remote,
        ):
            view.enrich_links_data([link1, link2], obj)

        assert mock_enrich_local.call_count == 2
        assert mock_remote.call_count == 1  # Only link1 has remote_device

    def test_enrich_links_data_sets_device_id(self):
        """Each link gets device_id set to obj.id."""
        view = self._make_view()

        link = {"local_port": "Gi0/0", "local_port_id": 1, "remote_device": None}
        obj = _mock_obj()
        obj.id = 55

        with patch.object(view, "enrich_local_port"):
            view.enrich_links_data([link], obj)

        assert link["device_id"] == 55

    def test_check_cable_status_called_when_remote_device_resolved(self):
        """check_cable_status is called when remote device resolves successfully."""
        view = self._make_view()

        link = {
            "local_port": "Gi0/0",
            "local_port_id": 1,
            "remote_device": "sw-b",
            "remote_device_id": 5,
        }
        enriched = dict(link)
        enriched["netbox_remote_device_id"] = 10  # resolved

        obj = _mock_obj()

        with (
            patch.object(view, "enrich_local_port"),
            patch.object(view, "process_remote_device", return_value=enriched),
            patch.object(view, "check_cable_status", return_value=enriched) as mock_cable,
        ):
            view.enrich_links_data([link], obj)

        mock_cable.assert_called_once()


class TestBaseCableTableViewPrepareContext:
    """Tests for BaseCableTableView._prepare_context."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view.request = _mock_request()
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view._librenms_api.cache_timeout = 300
        return view

    def test_fetch_fresh_none_links_returns_none(self):
        """When get_links_data returns None, _prepare_context returns None."""
        view = self._make_view()
        obj = _mock_obj()

        with patch.object(view, "get_links_data", return_value=None):
            result = view._prepare_context(view.request, obj, fetch_fresh=True)

        assert result is None

    def test_cache_miss_returns_none(self):
        """When cache has no data and fetch_fresh=False, returns None."""
        view = self._make_view()
        obj = _mock_obj()

        with (
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="key"),
        ):
            mock_cache.get.return_value = None
            result = view._prepare_context(view.request, obj, fetch_fresh=False)

        assert result is None

    def test_fetch_fresh_caches_and_returns_context(self):
        """Successful fresh fetch caches data and returns context dict."""
        view = self._make_view()
        obj = _mock_obj()

        links = [{"local_port": "Gi0/0"}]
        mock_table = MagicMock()
        mock_table.configure = MagicMock()

        with (
            patch.object(view, "get_links_data", return_value=links),
            patch.object(view, "enrich_links_data", return_value=links),
            patch.object(view, "get_cache_key", return_value="cable-key"),
            patch.object(view, "get_table", return_value=mock_table),
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.cables_view.timezone") as mock_tz,
        ):
            mock_cache.ttl.return_value = 300
            mock_tz.now.return_value = MagicMock()
            mock_tz.timedelta.return_value = MagicMock()
            result = view._prepare_context(view.request, obj, fetch_fresh=True)

        assert result is not None
        assert result["object"] is obj
        assert result["table"] is mock_table
        mock_cache.set.assert_called()

    def test_cache_hit_re_enriches_and_returns_context(self):
        """Cached data is re-enriched and returned."""
        view = self._make_view()
        obj = _mock_obj()

        cached_links = [{"local_port": "Gi0/0", "remote_device": "sw-b"}]
        mock_table = MagicMock()
        mock_table.configure = MagicMock()

        with (
            patch.object(view, "get_cache_key", return_value="cable-key"),
            patch.object(view, "enrich_links_data", return_value=cached_links),
            patch.object(view, "get_table", return_value=mock_table),
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.cables_view.timezone") as mock_tz,
        ):
            mock_cache.get.return_value = {"links": cached_links}
            mock_cache.ttl.return_value = 200
            mock_tz.now.return_value = MagicMock()
            mock_tz.timedelta.return_value = MagicMock()
            result = view._prepare_context(view.request, obj, fetch_fresh=False)

        assert result is not None
        assert result["object"] is obj


class TestBaseCableTableViewGetContextData:
    """Tests for BaseCableTableView.get_context_data."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_returns_empty_context_when_no_cache(self):
        """When _prepare_context returns None, get_context_data returns fallback context."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        with patch.object(view, "_prepare_context", return_value=None):
            ctx = view.get_context_data(request, obj)

        assert ctx["table"] is None
        assert ctx["object"] is obj
        assert ctx["cache_expiry"] is None

    def test_returns_context_when_cache_populated(self):
        """When _prepare_context returns data, get_context_data returns it."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        fake_context = {"table": MagicMock(), "object": obj, "cache_expiry": None, "server_key": "default"}

        with patch.object(view, "_prepare_context", return_value=fake_context):
            ctx = view.get_context_data(request, obj)

        assert ctx is fake_context


class TestBaseCableTableViewPost:
    """Tests for BaseCableTableView.post."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view.model = MagicMock()
        return view

    def test_post_no_links_shows_error_and_renders(self):
        """When no links found, renders template with error message."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        with (
            patch.object(view, "get_object", return_value=obj),
            patch.object(view, "_prepare_context", return_value=None),
            patch("netbox_librenms_plugin.views.base.cables_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.base.cables_view.render") as mock_render,
        ):
            mock_render.return_value = MagicMock()
            view.post(request, pk=1)

        mock_messages.error.assert_called_once()
        mock_render.assert_called_once()

    def test_post_success_shows_message_and_renders(self):
        """When links found, renders template with success message."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()
        fake_context = {"table": MagicMock(), "object": obj, "cache_expiry": None}

        with (
            patch.object(view, "get_object", return_value=obj),
            patch.object(view, "_prepare_context", return_value=fake_context),
            patch("netbox_librenms_plugin.views.base.cables_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.base.cables_view.render") as mock_render,
        ):
            mock_render.return_value = MagicMock()
            view.post(request, pk=1)

        mock_messages.success.assert_called_once()
        mock_render.assert_called_once()

    def test_get_ports_data_uses_cache_when_available(self):
        """get_ports_data returns cached data when present."""
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view.librenms_id = 42

        cached = {"ports": [{"port_id": 1, "ifName": "Gi0/0"}]}
        obj = _mock_obj()

        with (
            patch.object(view, "get_cache_key", return_value="ports-key"),
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
        ):
            mock_cache.get.return_value = cached
            result = view.get_ports_data(obj)

        assert result is cached

    def test_get_ports_data_fetches_from_api_on_cache_miss(self):
        """get_ports_data calls API when cache is empty."""
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view.librenms_id = 42

        api_data = {"ports": [{"port_id": 2}]}
        view._librenms_api.get_ports.return_value = (True, api_data)

        obj = _mock_obj()

        with (
            patch.object(view, "get_cache_key", return_value="ports-key"),
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
        ):
            mock_cache.get.return_value = None
            result = view.get_ports_data(obj)

        assert result is api_data


# =============================================================================
# BaseInterfaceTableView
# =============================================================================


class TestBaseInterfaceTableViewBasics:
    """Tests for BaseInterfaceTableView utility methods."""

    def _make_view(self, model_name="device"):
        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        view = object.__new__(BaseInterfaceTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view._librenms_api.cache_timeout = 300
        view.model = MagicMock()
        view.model.__name__ = model_name
        view.interface_name_field = None
        return view

    def test_get_ip_address_with_primary_ip(self):
        """Returns string IP when primary_ip is set."""
        view = self._make_view()
        obj = MagicMock()
        obj.primary_ip.address.ip = "192.168.1.1"
        result = view.get_ip_address(obj)
        assert result == "192.168.1.1"

    def test_get_ip_address_without_primary_ip(self):
        """Returns None when primary_ip is falsy."""
        view = self._make_view()
        obj = MagicMock()
        obj.primary_ip = None
        result = view.get_ip_address(obj)
        assert result is None

    def test_get_select_related_field_for_vm(self):
        """Returns 'virtual_machine' for VirtualMachine model."""
        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        view = object.__new__(BaseInterfaceTableView)
        view.model = MagicMock()
        view.model.__name__ = "virtualmachine"
        obj = MagicMock()

        result = view.get_select_related_field(obj)

        assert result == "virtual_machine"

    def test_get_select_related_field_for_device(self):
        """Returns 'device' for Device model."""
        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        view = object.__new__(BaseInterfaceTableView)
        view.model = MagicMock()
        view.model.__name__ = "device"
        obj = MagicMock()

        result = view.get_select_related_field(obj)

        assert result == "device"

    def test_enrich_ports_calls_parse_per_port(self):
        """_enrich_ports_with_vlan_data calls parse_port_vlan_data for each port."""
        view = self._make_view()
        view._librenms_api.parse_port_vlan_data.side_effect = lambda p, f: {"parsed": True}

        ports = [{"port_id": 1, "ifName": "Gi0/0"}, {"port_id": 2, "ifName": "Gi0/1"}]
        result = view._enrich_ports_with_vlan_data(ports, "ifName")

        assert len(result) == 2
        assert view._librenms_api.parse_port_vlan_data.call_count == 2


class TestBaseInterfaceTableViewPost:
    """Tests for BaseInterfaceTableView.post."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        view = object.__new__(BaseInterfaceTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view._librenms_api.cache_timeout = 300
        view.model = MagicMock()
        view.partial_template_name = "test_template.html"
        return view

    def test_post_no_librenms_id_redirects_with_error(self):
        """When librenms_id not found, error message and redirect."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        view._librenms_api.get_librenms_id.return_value = None

        with (
            patch.object(view, "get_object", return_value=obj),
            patch.object(view, "get_redirect_url", return_value="/device/1/"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.base.interfaces_view.redirect") as mock_redirect,
        ):
            mock_redirect.return_value = MagicMock()
            view.post(request, pk=1)

        mock_messages.error.assert_called_once()
        mock_redirect.assert_called_once_with("/device/1/")

    def test_post_api_error_redirects_with_error(self):
        """When API returns failure, error message and redirect."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.get_ports.return_value = (False, "Connection refused")

        with (
            patch.object(view, "get_object", return_value=obj),
            patch.object(view, "get_redirect_url", return_value="/device/1/"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.base.interfaces_view.redirect") as mock_redirect,
        ):
            mock_redirect.return_value = MagicMock()
            view.post(request, pk=1)

        mock_messages.error.assert_called_once_with(request, "Connection refused")
        mock_redirect.assert_called_once_with("/device/1/")

    def test_post_success_caches_and_renders(self):
        """Successful fetch caches data and renders template."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.get_ports.return_value = (True, {"ports": [{"port_id": 1, "ifName": "Gi0/0"}]})

        with (
            patch.object(view, "get_object", return_value=obj),
            patch.object(view, "get_redirect_url", return_value="/device/1/"),
            patch.object(view, "_enrich_ports_with_vlan_data", return_value=[]),
            patch.object(view, "get_context_data", return_value={}),
            patch.object(view, "get_cache_key", return_value="cache-key") as mock_get_cache_key,
            patch.object(view, "get_last_fetched_key", return_value="last-key") as mock_get_last_fetched_key,
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.base.interfaces_view.render") as mock_render,
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.interfaces_view.timezone"),
        ):
            mock_render.return_value = MagicMock()
            view.post(request, pk=1)

        mock_messages.success.assert_called_once()
        mock_render.assert_called_once()
        mock_cache.set.assert_called()
        # Verify server_key is forwarded to cache key helpers (server-specific namespacing)
        mock_get_cache_key.assert_called_with(obj, "ports", "default")
        mock_get_last_fetched_key.assert_called_with(obj, "ports", "default")


class TestBaseInterfaceTableViewGetContextData:
    """Tests for BaseInterfaceTableView.get_context_data."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        view = object.__new__(BaseInterfaceTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view.model = MagicMock()
        view.model.__name__ = "device"
        view.interface_name_field = None
        return view

    def test_cache_miss_returns_empty_table(self):
        """When no cached data, table is None."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        with (
            patch.object(view, "get_cache_key", return_value="key"),
            patch.object(view, "get_last_fetched_key", return_value="last-key"),
            patch.object(view, "get_vlan_overrides_key", return_value="overrides-key"),
            patch.object(view, "get_vlan_groups_for_device", return_value=[]),
            patch.object(view, "_build_vlan_lookup_maps", return_value={}),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.interfaces_view.timezone"),
        ):
            mock_cache.get.return_value = None
            mock_cache.ttl.return_value = None
            ctx = view.get_context_data(request, obj, "ifName")

        assert ctx["table"] is None

    def test_cache_hit_non_vc_builds_table(self):
        """Cached data without VC produces table."""
        view = self._make_view()
        obj = _mock_obj()
        obj.virtual_chassis = None
        request = _mock_request()

        cached_data = {
            "ports": [{"port_id": 1, "ifName": "Gi0/0", "ifAdminStatus": "up", "ifAlias": None, "ifDescr": "Gi0/0"}]
        }

        mock_iface = MagicMock()
        mock_iface.name = "Gi0/0"
        mock_ifaces_qs = MagicMock()
        mock_ifaces_qs.select_related.return_value = [mock_iface]

        mock_table = MagicMock()
        mock_table.configure = MagicMock()

        with (
            patch.object(view, "get_cache_key", return_value="key"),
            patch.object(view, "get_last_fetched_key", return_value="last-key"),
            patch.object(view, "get_vlan_overrides_key", return_value="overrides-key"),
            patch.object(view, "get_vlan_groups_for_device", return_value=[]),
            patch.object(view, "_build_vlan_lookup_maps", return_value={"vid_to_groups": {}, "vid_to_vlans": {}}),
            patch.object(view, "get_interfaces", return_value=mock_ifaces_qs),
            patch.object(view, "_add_vlan_group_selection"),
            patch.object(view, "_add_missing_vlans_info"),
            patch.object(view, "get_table", return_value=mock_table),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.interfaces_view.timezone") as mock_tz,
        ):
            mock_cache.get.side_effect = lambda key: cached_data if key == "key" else None
            mock_cache.ttl.return_value = 300
            mock_tz.now.return_value = MagicMock()
            mock_tz.timedelta.return_value = MagicMock()
            ctx = view.get_context_data(request, obj, "ifName")

        assert ctx["table"] is mock_table

    def test_cache_hit_with_vc_uses_vc_members(self):
        """Cached data with VC queries each chassis member's interfaces."""
        view = self._make_view()

        vc = MagicMock()
        member1 = MagicMock()
        member1.id = 10
        member2 = MagicMock()
        member2.id = 11
        vc.members.all.return_value = [member1, member2]

        obj = _mock_obj()
        obj.virtual_chassis = vc
        obj.id = 9999  # distinct from all member IDs so VC path is unambiguous
        request = _mock_request()

        cached_data = {
            "ports": [{"port_id": 1, "ifName": "Gi0/0", "ifAdminStatus": "up", "ifAlias": "test", "ifDescr": "Gi0/0"}]
        }

        mock_iface_qs = MagicMock()
        mock_iface_qs.select_related.return_value = []

        mock_table = MagicMock()
        mock_table.configure = MagicMock()

        with (
            patch.object(view, "get_cache_key", return_value="key"),
            patch.object(view, "get_last_fetched_key", return_value="last-key"),
            patch.object(view, "get_vlan_overrides_key", return_value="overrides-key"),
            patch.object(view, "get_vlan_groups_for_device", return_value=[]),
            patch.object(view, "_build_vlan_lookup_maps", return_value={"vid_to_groups": {}, "vid_to_vlans": {}}),
            patch.object(view, "get_interfaces", return_value=mock_iface_qs),
            patch.object(view, "_add_vlan_group_selection"),
            patch.object(view, "_add_missing_vlans_info"),
            patch.object(view, "get_table", return_value=mock_table),
            patch(
                "netbox_librenms_plugin.views.base.interfaces_view.get_virtual_chassis_member",
                return_value=member1,
            ) as mock_get_vc_member,
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.interfaces_view.timezone") as mock_tz,
        ):
            mock_cache.get.side_effect = lambda key: cached_data if key == "key" else None
            mock_cache.ttl.return_value = 300
            mock_tz.now.return_value = MagicMock()
            mock_tz.timedelta.return_value = MagicMock()
            ctx = view.get_context_data(request, obj, "ifName")

        # VC members should be included
        assert ctx["virtual_chassis_members"] is not None
        # get_virtual_chassis_member should have been called with obj and the port name
        mock_get_vc_member.assert_called_once_with(obj, "Gi0/0")


class TestBaseInterfaceTableViewAddVlanGroupSelection:
    """Tests for BaseInterfaceTableView._add_vlan_group_selection."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        view = object.__new__(BaseInterfaceTableView)
        view._librenms_api = MagicMock()
        return view

    def test_no_vlans_produces_empty_map(self):
        """Port with no tagged or untagged VLANs gets empty vlan_group_map."""
        view = self._make_view()
        port = {"untagged_vlan": None, "tagged_vlans": []}
        lookup_maps = {"vid_to_groups": {}}
        device = MagicMock()

        view._add_vlan_group_selection(port, lookup_maps, device)

        assert port["vlan_group_map"] == {}

    def test_single_group_for_vid_maps_directly(self):
        """When exactly one group contains the VID, it maps without ambiguity."""
        view = self._make_view()

        group = MagicMock()
        group.pk = 1
        group.name = "Corp"

        port = {"untagged_vlan": 100, "tagged_vlans": []}
        lookup_maps = {"vid_to_groups": {100: [group]}}
        device = MagicMock()

        view._add_vlan_group_selection(port, lookup_maps, device)

        assert port["vlan_group_map"][100]["group_id"] == "1"
        assert port["vlan_group_map"][100]["is_ambiguous"] is False

    def test_multiple_groups_with_most_specific_winner(self):
        """Multiple groups: _select_most_specific_group determines winner."""
        view = self._make_view()

        group_a = MagicMock()
        group_a.pk = 1
        group_a.name = "Rack-Group"

        group_b = MagicMock()
        group_b.pk = 2
        group_b.name = "Site-Group"

        port = {"untagged_vlan": 50, "tagged_vlans": []}
        lookup_maps = {"vid_to_groups": {50: [group_a, group_b]}}
        device = MagicMock()

        with patch.object(view, "_select_most_specific_group", return_value=group_a):
            view._add_vlan_group_selection(port, lookup_maps, device)

        assert port["vlan_group_map"][50]["group_id"] == "1"
        assert port["vlan_group_map"][50]["is_ambiguous"] is False

    def test_multiple_groups_no_winner_marks_ambiguous(self):
        """Multiple groups with no clear winner produces is_ambiguous=True."""
        view = self._make_view()

        group_a = MagicMock()
        group_b = MagicMock()

        port = {"untagged_vlan": 50, "tagged_vlans": []}
        lookup_maps = {"vid_to_groups": {50: [group_a, group_b]}}
        device = MagicMock()

        with patch.object(view, "_select_most_specific_group", return_value=None):
            view._add_vlan_group_selection(port, lookup_maps, device)

        assert port["vlan_group_map"][50]["is_ambiguous"] is True

    def test_vid_not_in_any_group_gets_global_entry(self):
        """VID not found in vid_to_groups produces group_id='' and group_name='Global'."""
        view = self._make_view()
        port = {"untagged_vlan": 999, "tagged_vlans": []}
        lookup_maps = {"vid_to_groups": {}}
        device = MagicMock()

        view._add_vlan_group_selection(port, lookup_maps, device)

        assert port["vlan_group_map"][999]["group_id"] == ""
        assert port["vlan_group_map"][999]["group_name"] == "Global"

    def test_vlan_group_overrides_applied(self):
        """vlan_group_overrides replace auto-selection for matching VIDs."""
        view = self._make_view()

        override_group = MagicMock()
        override_group.pk = 99
        override_group.name = "Override-Group"

        port = {"untagged_vlan": 100, "tagged_vlans": []}
        lookup_maps = {"vid_to_groups": {}}
        device = MagicMock()

        with patch("ipam.models.VLANGroup") as MockVLANGroup:
            MockVLANGroup.objects.in_bulk.return_value = {99: override_group}
            view._add_vlan_group_selection(port, lookup_maps, device, vlan_group_overrides={"100": "99"})

        assert port["vlan_group_map"][100]["group_id"] == "99"
        assert port["vlan_group_map"][100]["group_name"] == "Override-Group"

    def test_override_with_empty_string_forces_global(self):
        """Override with empty string means 'No Group (Global)'."""
        view = self._make_view()

        port = {"untagged_vlan": 100, "tagged_vlans": []}
        lookup_maps = {"vid_to_groups": {}}
        device = MagicMock()

        with patch("ipam.models.VLANGroup") as MockVLANGroup:
            MockVLANGroup.objects.in_bulk.return_value = {}
            view._add_vlan_group_selection(port, lookup_maps, device, vlan_group_overrides={"100": ""})

        assert port["vlan_group_map"][100]["group_id"] == ""
        assert port["vlan_group_map"][100]["group_name"] == "Global"


class TestBaseInterfaceTableViewAddMissingVlansInfo:
    """Tests for BaseInterfaceTableView._add_missing_vlans_info."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        view = object.__new__(BaseInterfaceTableView)
        return view

    def test_all_vlans_found_empty_missing(self):
        """When all VIDs are in vid_to_vlans, missing_vlans is empty."""
        view = self._make_view()
        port = {"untagged_vlan": 10, "tagged_vlans": [20, 30]}
        lookup_maps = {"vid_to_vlans": {10: [MagicMock()], 20: [MagicMock()], 30: [MagicMock()]}}

        view._add_missing_vlans_info(port, lookup_maps)

        assert port["missing_vlans"] == []

    def test_missing_untagged_vlan_added(self):
        """Untagged VID not in vid_to_vlans appears in missing_vlans."""
        view = self._make_view()
        port = {"untagged_vlan": 99, "tagged_vlans": []}
        lookup_maps = {"vid_to_vlans": {}}

        view._add_missing_vlans_info(port, lookup_maps)

        assert 99 in port["missing_vlans"]

    def test_missing_tagged_vlans_added(self):
        """Tagged VIDs not in vid_to_vlans appear in missing_vlans."""
        view = self._make_view()
        port = {"untagged_vlan": None, "tagged_vlans": [100, 200]}
        lookup_maps = {"vid_to_vlans": {100: [MagicMock()]}}  # 200 is missing

        view._add_missing_vlans_info(port, lookup_maps)

        assert 200 in port["missing_vlans"]
        assert 100 not in port["missing_vlans"]

    def test_no_vlans_produces_empty_missing(self):
        """Port with no VLANs results in empty missing_vlans."""
        view = self._make_view()
        port = {"untagged_vlan": None, "tagged_vlans": []}
        lookup_maps = {"vid_to_vlans": {}}

        view._add_missing_vlans_info(port, lookup_maps)

        assert port["missing_vlans"] == []


# =============================================================================
# BaseIPAddressTableView
# =============================================================================


class TestBaseIPAddressTableViewCreateBaseIpEntry:
    """Tests for BaseIPAddressTableView._create_base_ip_entry."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        view = object.__new__(BaseIPAddressTableView)
        view._librenms_api = MagicMock()
        return view

    def test_unified_format_preferred(self):
        """When ip_address/prefix_length present, those fields are used."""
        view = self._make_view()
        ip_entry = {"ip_address": "10.0.0.1", "prefix_length": 24, "port_id": 5}
        obj = MagicMock()
        obj.name = "router"
        obj.get_absolute_url.return_value = "/dcim/devices/1/"

        result = view._create_base_ip_entry(ip_entry, obj, vrfs=[])

        assert result["ip_address"] == "10.0.0.1"
        assert result["prefix_length"] == 24
        assert result["ip_with_mask"] == "10.0.0.1/24"

    def test_legacy_ipv4_format(self):
        """Legacy ipv4_address/ipv4_prefixlen format is handled."""
        view = self._make_view()
        ip_entry = {"ipv4_address": "192.168.1.100", "ipv4_prefixlen": 24, "port_id": 10}
        obj = MagicMock()
        obj.name = "router"
        obj.get_absolute_url.return_value = "/dcim/devices/1/"

        result = view._create_base_ip_entry(ip_entry, obj, vrfs=[])

        assert result["ip_address"] == "192.168.1.100"
        assert result["ip_with_mask"] == "192.168.1.100/24"

    def test_legacy_ipv6_format(self):
        """Legacy ipv6_compressed/ipv6_prefixlen format is handled."""
        view = self._make_view()
        ip_entry = {"ipv6_compressed": "2001:db8::1", "ipv6_prefixlen": 64, "port_id": 15}
        obj = MagicMock()
        obj.name = "router"
        obj.get_absolute_url.return_value = "/dcim/devices/1/"

        result = view._create_base_ip_entry(ip_entry, obj, vrfs=[])

        assert result["ip_address"] == "2001:db8::1"
        assert result["ip_with_mask"] == "2001:db8::1/64"

    def test_no_valid_format_raises_key_error(self):
        """When no valid IP format is found, KeyError is raised."""
        import pytest

        view = self._make_view()
        ip_entry = {"port_id": 1}  # No IP address fields
        obj = MagicMock()

        with pytest.raises(KeyError):
            view._create_base_ip_entry(ip_entry, obj, vrfs=[])


class TestBaseIPAddressTableViewEnrichIpData:
    """Tests for BaseIPAddressTableView.enrich_ip_data."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        view = object.__new__(BaseIPAddressTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_non_dict_entries_skipped(self):
        """Non-dict items in ip_data are silently skipped."""
        view = self._make_view()
        obj = _mock_obj()

        with patch.object(
            view,
            "_prefetch_netbox_data",
            return_value={
                "vrfs": [],
                "ip_addresses_map": {},
                "interfaces_by_librenms_id": {},
                "interfaces_by_name": {},
                "all_interfaces": [],
                "device": obj,
            },
        ):
            result = view.enrich_ip_data(["not-a-dict", 42, None], obj, "ifName")

        assert result == []

    def test_entries_without_port_id_skipped(self):
        """Entries missing port_id field are skipped."""
        view = self._make_view()
        obj = _mock_obj()
        ip_data = [{"ip_address": "10.0.0.1", "prefix_length": 24}]  # no port_id

        with patch.object(
            view,
            "_prefetch_netbox_data",
            return_value={
                "vrfs": [],
                "ip_addresses_map": {},
                "interfaces_by_librenms_id": {},
                "interfaces_by_name": {},
                "all_interfaces": [],
                "device": obj,
            },
        ):
            result = view.enrich_ip_data(ip_data, obj, "ifName")

        assert result == []

    def test_new_ip_gets_sync_status(self):
        """IP not in NetBox gets exists=False, status='sync'."""
        view = self._make_view()
        obj = _mock_obj()

        ip_data = [{"ip_address": "10.1.1.1", "prefix_length": 24, "port_id": 10}]
        prefetched = {
            "vrfs": [],
            "ip_addresses_map": {},
            "interfaces_by_librenms_id": {},
            "interfaces_by_name": {},
            "all_interfaces": [],
            "device": obj,
        }

        with (
            patch.object(view, "_prefetch_netbox_data", return_value=prefetched),
            patch.object(view, "_get_port_info", return_value=None),
            patch.object(view, "_add_interface_info_to_ip"),
        ):
            result = view.enrich_ip_data(ip_data, obj, "ifName")

        assert len(result) == 1
        assert result[0]["exists"] is False
        assert result[0]["status"] == "sync"

    def test_existing_ip_gets_enriched(self):
        """IP that exists in NetBox goes through enrich_existing path."""
        view = self._make_view()
        obj = _mock_obj()

        existing_ip = MagicMock()
        existing_ip.get_absolute_url.return_value = "/ipam/ip-addresses/1/"
        existing_ip.vrf = None
        existing_ip.assigned_object = None

        ip_data = [{"ip_address": "192.168.1.1", "prefix_length": 24, "port_id": 20}]
        prefetched = {
            "vrfs": [],
            "ip_addresses_map": {"192.168.1.1/24": existing_ip},
            "interfaces_by_librenms_id": {},
            "interfaces_by_name": {},
            "all_interfaces": [],
            "device": obj,
        }

        with (
            patch.object(view, "_prefetch_netbox_data", return_value=prefetched),
            patch.object(view, "_get_port_info", return_value=None),
            patch.object(view, "_enrich_existing_ip") as mock_enrich,
            patch.object(view, "_add_interface_info_to_ip"),
        ):
            view.enrich_ip_data(ip_data, obj, "ifName")

        mock_enrich.assert_called_once()


class TestBaseIPAddressTableViewGetPortInfo:
    """Tests for BaseIPAddressTableView._get_port_info."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        view = object.__new__(BaseIPAddressTableView)
        view._librenms_api = MagicMock()
        return view

    def test_cache_miss_calls_api(self):
        """First call for a port_id queries the API."""
        view = self._make_view()
        port_data = {"port": [{"port_id": 10, "ifName": "Gi0/0"}]}
        view._librenms_api.get_port_by_id.return_value = (True, port_data)

        cache = {}
        result = view._get_port_info(10, cache, "ifName")

        assert result == port_data["port"][0]
        view._librenms_api.get_port_by_id.assert_called_once_with(10)

    def test_cache_hit_skips_api(self):
        """Subsequent call for same port_id uses cache."""
        view = self._make_view()
        cached_port = {"port_id": 10, "ifName": "Gi0/0"}
        cache = {10: cached_port}

        result = view._get_port_info(10, cache, "ifName")

        assert result is cached_port
        view._librenms_api.get_port_by_id.assert_not_called()

    def test_api_failure_caches_none(self):
        """When API fails, caches None and returns None."""
        view = self._make_view()
        view._librenms_api.get_port_by_id.return_value = (False, {})

        cache = {}
        result = view._get_port_info(99, cache, "ifName")

        assert result is None
        assert cache[99] is None

    def test_api_empty_port_list_caches_none(self):
        """When API returns empty port list, caches None."""
        view = self._make_view()
        view._librenms_api.get_port_by_id.return_value = (True, {"port": []})

        cache = {}
        result = view._get_port_info(99, cache, "ifName")

        assert result is None


class TestBaseIPAddressTableViewEnrichExistingIp:
    """Tests for BaseIPAddressTableView._enrich_existing_ip."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        view = object.__new__(BaseIPAddressTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_matched_by_librenms_id(self):
        """IP matched by LibreNMS port_id → status='matched'."""
        view = self._make_view()

        assigned_iface = MagicMock()
        existing_ip = MagicMock()
        existing_ip.vrf = None
        existing_ip.assigned_object = assigned_iface
        existing_ip.get_absolute_url.return_value = "/ipam/ip-addresses/1/"

        prefetched = {
            "interfaces_by_librenms_id": {"10": assigned_iface},
        }

        enriched = {}
        view._enrich_existing_ip(enriched, existing_ip, 10, "Gi0/0", prefetched)

        assert enriched["status"] == "matched"

    def test_matched_by_interface_name(self):
        """IP matched by interface name → status='matched'."""
        view = self._make_view()

        assigned_iface = MagicMock()
        assigned_iface.name = "Gi0/0"
        assigned_iface.get_absolute_url.return_value = "/dcim/interfaces/5/"
        existing_ip = MagicMock()
        existing_ip.vrf = None
        existing_ip.assigned_object = assigned_iface
        existing_ip.get_absolute_url.return_value = "/ipam/ip-addresses/1/"

        prefetched = {
            "interfaces_by_librenms_id": {},
        }

        enriched = {}
        view._enrich_existing_ip(enriched, existing_ip, 10, "Gi0/0", prefetched)

        assert enriched["status"] == "matched"

    def test_update_status_when_not_matched(self):
        """IP exists but interface doesn't match → status='update'."""
        view = self._make_view()

        other_iface = MagicMock()
        other_iface.name = "Gi0/1"  # Different from librenms interface
        existing_ip = MagicMock()
        existing_ip.vrf = None
        existing_ip.assigned_object = other_iface
        existing_ip.get_absolute_url.return_value = "/ipam/ip-addresses/1/"

        prefetched = {
            "interfaces_by_librenms_id": {},  # No librenms_id match
        }

        enriched = {}
        view._enrich_existing_ip(enriched, existing_ip, 10, "Gi0/0", prefetched)

        assert enriched["status"] == "update"

    def test_vrf_info_added_when_present(self):
        """VRF info is added to enriched_ip when IP has a VRF."""
        view = self._make_view()

        vrf = MagicMock()
        vrf.pk = 1
        vrf.name = "MGMT-VRF"

        existing_ip = MagicMock()
        existing_ip.vrf = vrf
        existing_ip.assigned_object = None
        existing_ip.get_absolute_url.return_value = "/ipam/ip-addresses/1/"

        prefetched = {"interfaces_by_librenms_id": {}}

        enriched = {}
        view._enrich_existing_ip(enriched, existing_ip, 10, None, prefetched)

        assert enriched["vrf_id"] == 1
        assert enriched["vrf"] == "MGMT-VRF"

    def test_not_assigned_returns_update_early(self):
        """When IP is not assigned to any object, returns early with status='update'."""
        view = self._make_view()

        existing_ip = MagicMock()
        existing_ip.vrf = None
        existing_ip.assigned_object = None
        existing_ip.get_absolute_url.return_value = "/ipam/ip-addresses/1/"

        prefetched = {"interfaces_by_librenms_id": {}}

        enriched = {}
        view._enrich_existing_ip(enriched, existing_ip, 10, "Gi0/0", prefetched)

        assert enriched["status"] == "update"


class TestBaseIPAddressTableViewAddInterfaceInfo:
    """Tests for BaseIPAddressTableView._add_interface_info_to_ip."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        view = object.__new__(BaseIPAddressTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_matches_by_librenms_id_first(self):
        """Interface matched by librenms_id takes priority over name match."""
        view = self._make_view()

        iface_by_id = MagicMock()
        iface_by_id.name = "Gi0/0"
        iface_by_id.get_absolute_url.return_value = "/dcim/interfaces/1/"

        prefetched = {
            "interfaces_by_librenms_id": {"10": iface_by_id},
            "interfaces_by_name": {},
        }

        enriched = {}
        view._add_interface_info_to_ip(enriched, 10, "Gi0/0", prefetched)

        assert enriched["interface_name"] == "Gi0/0"
        assert enriched["interface_url"] == "/dcim/interfaces/1/"

    def test_falls_back_to_name_match(self):
        """When no librenms_id match, falls back to interface name match."""
        view = self._make_view()

        iface_by_name = MagicMock()
        iface_by_name.get_absolute_url.return_value = "/dcim/interfaces/2/"

        prefetched = {
            "interfaces_by_librenms_id": {},
            "interfaces_by_name": {"Gi0/1": iface_by_name},
        }

        enriched = {}
        view._add_interface_info_to_ip(enriched, 20, "Gi0/1", prefetched)

        assert enriched["interface_url"] == "/dcim/interfaces/2/"

    def test_no_match_leaves_enriched_unchanged(self):
        """When no interface found, enriched dict is not modified."""
        view = self._make_view()

        prefetched = {
            "interfaces_by_librenms_id": {},
            "interfaces_by_name": {},
        }

        enriched = {"ip_address": "10.0.0.1"}
        view._add_interface_info_to_ip(enriched, 30, "Gi0/2", prefetched)

        assert "interface_url" not in enriched


class TestBaseIPAddressTableViewPrepareContext:
    """Tests for BaseIPAddressTableView._prepare_context."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        view = object.__new__(BaseIPAddressTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view._librenms_api.cache_timeout = 300
        return view

    def test_cache_miss_fetch_fresh_false_returns_none(self):
        """When no cached data and fetch_fresh=False, returns None."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        with (
            patch.object(view, "get_cache_key", return_value="ip-key"),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.views.base.ip_addresses_view.get_interface_name_field",
                return_value="ifName",
            ),
        ):
            mock_cache.get.return_value = None
            result = view._prepare_context(request, obj, "ifName", fetch_fresh=False)

        assert result is None

    def test_fetch_fresh_caches_enriched_data(self):
        """When fetch_fresh=True, IP data is fetched, enriched, cached and returned."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        raw_ips = [{"ip_address": "10.0.0.1", "prefix_length": 24, "port_id": 1}]
        enriched_ips = [{"ip_with_mask": "10.0.0.1/24", "status": "sync"}]
        mock_table = MagicMock()
        mock_table.configure = MagicMock()

        with (
            patch.object(view, "get_ip_addresses", return_value=(True, raw_ips)),
            patch.object(view, "enrich_ip_data", return_value=enriched_ips),
            patch.object(view, "get_table", return_value=mock_table),
            patch.object(view, "get_cache_key", return_value="ip-key"),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.timezone") as mock_tz,
            patch(
                "netbox_librenms_plugin.views.base.ip_addresses_view.get_interface_name_field",
                return_value="ifName",
            ),
        ):
            mock_cache.ttl.return_value = 300
            mock_tz.now.return_value = MagicMock()
            mock_tz.timedelta.return_value = MagicMock()
            result = view._prepare_context(request, obj, "ifName", fetch_fresh=True)

        assert result is not None
        assert result["table"] is mock_table
        mock_cache.set.assert_called()

    def test_cache_hit_fetch_fresh_false_uses_cached_data(self):
        """Cached data available with fetch_fresh=False returns context."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        cached_ips = [{"ip_with_mask": "192.168.1.1/24", "status": "matched"}]
        mock_table = MagicMock()
        mock_table.configure = MagicMock()

        with (
            patch.object(view, "get_cache_key", return_value="ip-key"),
            patch.object(view, "enrich_ip_data", return_value=cached_ips),
            patch.object(view, "get_table", return_value=mock_table),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.timezone") as mock_tz,
            patch(
                "netbox_librenms_plugin.views.base.ip_addresses_view.get_interface_name_field",
                return_value="ifName",
            ),
        ):
            mock_cache.get.return_value = {"ip_addresses": cached_ips}
            mock_cache.ttl.return_value = 200
            mock_tz.now.return_value = MagicMock()
            mock_tz.timedelta.return_value = MagicMock()
            result = view._prepare_context(request, obj, "ifName", fetch_fresh=False)

        assert result is not None
        assert result["table"] is mock_table


class TestBaseIPAddressTableViewGetContextData:
    """Tests for BaseIPAddressTableView.get_context_data."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        view = object.__new__(BaseIPAddressTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_returns_empty_context_when_no_cache(self):
        """When _prepare_context returns None, returns fallback context with table=None."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        with (
            patch.object(view, "_prepare_context", return_value=None),
            patch(
                "netbox_librenms_plugin.views.base.ip_addresses_view.get_interface_name_field",
                return_value="ifName",
            ),
        ):
            ctx = view.get_context_data(request, obj)

        assert ctx["table"] is None
        assert ctx["object"] is obj
        assert ctx["cache_expiry"] is None

    def test_returns_context_when_cache_populated(self):
        """When _prepare_context returns context, that context is returned."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        prepared = {"table": MagicMock(), "object": obj, "cache_expiry": None, "server_key": "default"}

        with (
            patch.object(view, "_prepare_context", return_value=prepared),
            patch(
                "netbox_librenms_plugin.views.base.ip_addresses_view.get_interface_name_field",
                return_value="ifName",
            ),
        ):
            ctx = view.get_context_data(request, obj)

        assert ctx is prepared


class TestBaseIPAddressTableViewPost:
    """Tests for BaseIPAddressTableView.post."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        view = object.__new__(BaseIPAddressTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view.model = MagicMock()
        view.partial_template_name = "test_template.html"
        return view

    def test_post_no_ips_renders_error(self):
        """When _prepare_context returns None, renders with error."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        with (
            patch.object(view, "get_object", return_value=obj),
            patch.object(view, "_prepare_context", return_value=None),
            patch(
                "netbox_librenms_plugin.views.base.ip_addresses_view.get_interface_name_field",
                return_value="ifName",
            ),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.render") as mock_render,
        ):
            mock_render.return_value = MagicMock()
            view.post(request, pk=1)

        mock_messages.error.assert_called_once()
        mock_render.assert_called_once()

    def test_post_success_renders_with_context(self):
        """Successful fetch renders template with context."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()
        fake_ctx = {"table": MagicMock(), "object": obj}

        with (
            patch.object(view, "get_object", return_value=obj),
            patch.object(view, "_prepare_context", return_value=fake_ctx),
            patch(
                "netbox_librenms_plugin.views.base.ip_addresses_view.get_interface_name_field",
                return_value="ifName",
            ),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.render") as mock_render,
        ):
            mock_render.return_value = MagicMock()
            view.post(request, pk=1)

        mock_messages.success.assert_called_once()
        mock_render.assert_called_once()
        render_call_kwargs = mock_render.call_args[0]
        assert "ip_sync" in render_call_kwargs[2]


class TestBaseIPAddressTableViewPrefetchNetboxData:
    """Tests for BaseIPAddressTableView._prefetch_netbox_data."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        view = object.__new__(BaseIPAddressTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_builds_lookup_maps(self):
        """_prefetch_netbox_data builds interface and IP lookup maps."""
        view = self._make_view()

        iface = MagicMock()
        iface.name = "Gi0/0"

        obj = MagicMock()
        obj.interfaces = MagicMock()
        obj.interfaces.all.return_value = [iface]

        with (
            patch(
                "netbox_librenms_plugin.views.base.ip_addresses_view.get_librenms_device_id",
                return_value=10,
            ) as mock_get_librenms_id,
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.IPAddress") as MockIP,
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.VRF") as MockVRF,
        ):
            MockIP.objects.select_related.return_value = []
            MockVRF.objects.all.return_value = []
            result = view._prefetch_netbox_data(obj)

        assert "interfaces_by_librenms_id" in result
        assert "interfaces_by_name" in result
        assert "ip_addresses_map" in result
        assert result["interfaces_by_name"]["Gi0/0"] is iface
        assert result["interfaces_by_librenms_id"]["10"] is iface
        # Verify server_key is forwarded so IDs are scoped per-server
        mock_get_librenms_id.assert_called_once_with(iface, "default", auto_save=False)


class TestBaseIPAddressTableViewGetTable:
    """Tests for BaseIPAddressTableView.get_table."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        view = object.__new__(BaseIPAddressTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view.request = _mock_request()
        return view

    def test_get_table_sets_htmx_url(self):
        """get_table creates table and sets htmx_url with tab parameter."""
        view = self._make_view()
        obj = _mock_obj()

        with patch("netbox_librenms_plugin.views.base.ip_addresses_view.IPAddressTable") as MockTable:
            mock_table = MagicMock()
            MockTable.return_value = mock_table
            view.get_table([], obj, view.request)

        assert isinstance(mock_table.htmx_url, str)
        assert "ipaddresses" in mock_table.htmx_url
        assert "server_key=default" in mock_table.htmx_url


# ===========================================================================
# BaseInterfaceTableView — missing line coverage (lines 31, 44, 51, 70, 149, 203, 224-230)
# ===========================================================================


class TestBaseInterfaceTableViewMissingLines:
    """Targeted tests for remaining uncovered lines in BaseInterfaceTableView."""

    def _make_view(self):
        from unittest.mock import MagicMock

        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        view = object.__new__(BaseInterfaceTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view._librenms_api.cache_timeout = 300
        view.model = MagicMock()
        view.model.__name__ = "device"
        view.interface_name_field = None
        return view

    def test_get_object_calls_get_object_or_404(self):
        """get_object delegates to get_object_or_404(self.model, pk=pk)."""
        from unittest.mock import MagicMock, patch

        view = self._make_view()
        mock_obj = MagicMock()

        with patch(
            "netbox_librenms_plugin.views.base.interfaces_view.get_object_or_404",
            return_value=mock_obj,
        ) as mock_404:
            result = view.get_object(42)

        mock_404.assert_called_once_with(view.model, pk=42)
        assert result is mock_obj

    def test_get_interfaces_raises_not_implemented(self):
        """get_interfaces raises NotImplementedError — must be overridden."""
        from unittest.mock import MagicMock

        view = self._make_view()
        try:
            view.get_interfaces(MagicMock())
            assert False, "Expected NotImplementedError"
        except NotImplementedError:
            pass

    def test_get_redirect_url_raises_not_implemented(self):
        """get_redirect_url raises NotImplementedError — must be overridden."""
        from unittest.mock import MagicMock

        view = self._make_view()
        try:
            view.get_redirect_url(MagicMock())
            assert False, "Expected NotImplementedError"
        except NotImplementedError:
            pass

    def test_get_table_raises_not_implemented(self):
        """get_table raises NotImplementedError — must be overridden."""
        from unittest.mock import MagicMock

        view = self._make_view()
        try:
            view.get_table([], MagicMock(), "ifName")
            assert False, "Expected NotImplementedError"
        except NotImplementedError:
            pass

    def test_get_context_data_with_none_interface_name_field_calls_helper(self):
        """When interface_name_field=None, get_interface_name_field(request) is called."""
        from unittest.mock import MagicMock, patch

        view = self._make_view()
        obj = MagicMock()
        obj.virtual_chassis = None
        obj.id = 1
        request = MagicMock()

        with (
            patch.object(view, "get_cache_key", return_value="k"),
            patch.object(view, "get_last_fetched_key", return_value="lk"),
            patch.object(view, "get_vlan_overrides_key", return_value="vk"),
            patch.object(view, "get_vlan_groups_for_device", return_value=[]),
            patch.object(view, "_build_vlan_lookup_maps", return_value={}),
            patch(
                "netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field",
                return_value="ifDescr",
            ) as mock_gnf,
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.interfaces_view.timezone"),
        ):
            mock_cache.get.return_value = None
            mock_cache.ttl.return_value = None
            view.get_context_data(request, obj, interface_name_field=None)

        mock_gnf.assert_called_once_with(request)

    def test_ifalias_cleared_when_matches_ifdescr(self):
        """port['ifAlias'] is set to '' when it equals ifDescr (line 203)."""
        from unittest.mock import MagicMock, patch

        view = self._make_view()
        obj = MagicMock()
        obj.virtual_chassis = None
        obj.id = 1
        request = MagicMock()

        ports_data = [
            {
                "port_id": 1,
                "ifName": "Gi0/0",
                "ifDescr": "GigabitEthernet0/0",
                "ifAlias": "GigabitEthernet0/0",  # matches ifDescr -> cleared
                "ifAdminStatus": "up",
            }
        ]
        cached_data = {"ports": ports_data}

        mock_iface = MagicMock()
        mock_iface.name = "Gi0/0"
        mock_ifaces_qs = MagicMock()
        mock_ifaces_qs.select_related.return_value = [mock_iface]

        mock_table = MagicMock()
        mock_table.configure = MagicMock()

        with (
            patch.object(view, "get_cache_key", return_value="k"),
            patch.object(view, "get_last_fetched_key", return_value="lk"),
            patch.object(view, "get_vlan_overrides_key", return_value="vk"),
            patch.object(view, "get_vlan_groups_for_device", return_value=[]),
            patch.object(view, "_build_vlan_lookup_maps", return_value={"vid_to_groups": {}, "vid_to_vlans": {}}),
            patch.object(view, "get_interfaces", return_value=mock_ifaces_qs),
            patch.object(view, "_add_vlan_group_selection"),
            patch.object(view, "_add_missing_vlans_info"),
            patch.object(view, "get_table", return_value=mock_table),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.interfaces_view.timezone") as mock_tz,
        ):
            mock_cache.get.side_effect = lambda key: cached_data if key == "k" else None
            mock_cache.ttl.return_value = 300
            mock_tz.now.return_value = MagicMock()
            mock_tz.timedelta.return_value = MagicMock()
            view.get_context_data(request, obj, "ifName")

        assert ports_data[0]["ifAlias"] == ""

    def test_netbox_only_interfaces_vc_device_name_lookup(self):
        """VC branch fetches device_name from VC members for netbox-only interfaces (lines 224-226)."""
        from unittest.mock import MagicMock, patch

        view = self._make_view()

        member1 = MagicMock()
        member1.id = 10
        member1.name = "switch-1"

        vc = MagicMock()
        vc.members.all.return_value = [member1]
        vc.members.get.return_value = member1

        obj = MagicMock()
        obj.virtual_chassis = vc
        obj.id = 10
        request = MagicMock()

        # Gi0/0 is in LibreNMS; Gi0/1 is only in NetBox
        cached_data = {"ports": [{"port_id": 1, "ifName": "Gi0/0", "ifAdminStatus": "up", "ifAlias": "x"}]}

        netbox_only_iface = MagicMock()
        netbox_only_iface.name = "Gi0/1"
        netbox_only_iface.id = 99
        netbox_only_iface.type = "1000base-t"

        mock_ifaces_qs = MagicMock()
        mock_ifaces_qs.select_related.return_value = [netbox_only_iface]

        mock_table = MagicMock()
        mock_table.configure = MagicMock()

        with (
            patch.object(view, "get_cache_key", return_value="k"),
            patch.object(view, "get_last_fetched_key", return_value="lk"),
            patch.object(view, "get_vlan_overrides_key", return_value="vk"),
            patch.object(view, "get_vlan_groups_for_device", return_value=[]),
            patch.object(view, "_build_vlan_lookup_maps", return_value={"vid_to_groups": {}, "vid_to_vlans": {}}),
            patch.object(view, "get_interfaces", return_value=mock_ifaces_qs),
            patch.object(view, "_add_vlan_group_selection"),
            patch.object(view, "_add_missing_vlans_info"),
            patch.object(view, "get_table", return_value=mock_table),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_virtual_chassis_member", return_value=member1),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.interfaces_view.timezone") as mock_tz,
        ):
            mock_cache.get.side_effect = lambda key: cached_data if key == "k" else None
            mock_cache.ttl.return_value = 300
            mock_tz.now.return_value = MagicMock()
            mock_tz.timedelta.return_value = MagicMock()
            ctx = view.get_context_data(request, obj, "ifName")

        netbox_only = ctx.get("netbox_only_interfaces", [])
        assert any(item["name"] == "Gi0/1" for item in netbox_only)
        gi01 = next(i for i in netbox_only if i["name"] == "Gi0/1")
        assert gi01["device_name"] == "switch-1"

    def test_netbox_only_interfaces_non_vc_device_name_from_obj(self):
        """Non-VC branch uses obj.name directly for device_name (line 228)."""
        from unittest.mock import MagicMock, patch

        view = self._make_view()
        obj = MagicMock()
        obj.virtual_chassis = None
        obj.id = 1
        obj.name = "router-1"
        request = MagicMock()

        # Gi0/0 in LibreNMS; Gi0/1 only in NetBox
        cached_data = {"ports": [{"port_id": 1, "ifName": "Gi0/0", "ifAdminStatus": "up", "ifAlias": None}]}

        netbox_only_iface = MagicMock()
        netbox_only_iface.name = "Gi0/1"
        netbox_only_iface.id = 55
        netbox_only_iface.type = "1000base-t"

        mock_ifaces_qs = MagicMock()
        mock_ifaces_qs.select_related.return_value = [netbox_only_iface]

        mock_table = MagicMock()
        mock_table.configure = MagicMock()

        with (
            patch.object(view, "get_cache_key", return_value="k"),
            patch.object(view, "get_last_fetched_key", return_value="lk"),
            patch.object(view, "get_vlan_overrides_key", return_value="vk"),
            patch.object(view, "get_vlan_groups_for_device", return_value=[]),
            patch.object(view, "_build_vlan_lookup_maps", return_value={"vid_to_groups": {}, "vid_to_vlans": {}}),
            patch.object(view, "get_interfaces", return_value=mock_ifaces_qs),
            patch.object(view, "_add_vlan_group_selection"),
            patch.object(view, "_add_missing_vlans_info"),
            patch.object(view, "get_table", return_value=mock_table),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.interfaces_view.timezone") as mock_tz,
        ):
            mock_cache.get.side_effect = lambda key: cached_data if key == "k" else None
            mock_cache.ttl.return_value = 300
            mock_tz.now.return_value = MagicMock()
            mock_tz.timedelta.return_value = MagicMock()
            ctx = view.get_context_data(request, obj, "ifName")

        netbox_only = ctx.get("netbox_only_interfaces", [])
        gi01 = next((i for i in netbox_only if i["name"] == "Gi0/1"), None)
        assert gi01 is not None
        assert gi01["device_name"] == "router-1"
