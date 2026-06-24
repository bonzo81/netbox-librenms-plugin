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

import pytest

from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_ip

# =============================================================================
# Helpers
# =============================================================================


def _mock_obj(model_name="device", pk=1, name="test-device"):
    obj = MagicMock()
    obj._meta = MagicMock()
    obj._meta.model_name = model_name
    obj.pk = pk
    obj.id = pk
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

    def test_get_links_data_records_fetch_error_for_caller(self):
        """A real fetch failure must record _links_fetch_error so the caller can surface the actual error instead of the generic 'No links found' message; a no-links result (empty list) must leave it None."""
        view = self._make_view()
        obj = _mock_obj()

        # Failure → error recorded.
        view._librenms_api.get_device_links.return_value = (False, {"error": "auth failed"})
        with patch.object(view, "get_ports_data", return_value={"ports": []}):
            assert view.get_links_data(obj) is None
        assert view._links_fetch_error == "auth failed"

    def test_get_links_data_treats_non_dict_payload_as_failure(self):
        """get_device_links() returns the raw JSON body, so a 200 can yield a list/null/ scalar."""
        view = self._make_view()
        obj = _mock_obj()

        for bad_payload in ([{"local_port_id": 1}], None, "oops"):
            view = self._make_view()  # fresh per case so each proves it sets the error itself
            view._librenms_api.get_device_links.return_value = (True, bad_payload)
            with patch.object(view, "get_ports_data", return_value={"ports": []}):
                assert view.get_links_data(obj) is None
            assert view._links_fetch_error is not None

    def test_get_links_data_treats_dict_with_malformed_links_as_failure(self):
        """A dict body whose 'links' is null/object (not a list) must also be a fetch failure — otherwise `for link in links` crashes or iterates dict keys."""
        view = self._make_view()
        obj = _mock_obj()

        for bad_links in (None, {}, {"a": 1}, "nope"):
            view = self._make_view()  # fresh per case so each proves it sets the error itself
            view._librenms_api.get_device_links.return_value = (True, {"links": bad_links})
            with patch.object(view, "get_ports_data", return_value={"ports": []}):
                assert view.get_links_data(obj) is None
            assert view._links_fetch_error is not None

        # Success with no links → no error recorded (reset per call).
        view._librenms_api.get_device_links.return_value = (True, {"links": []})
        with (
            patch.object(view, "get_ports_data", return_value={"ports": []}),
            patch("netbox_librenms_plugin.views.base.cables_view.get_interface_name_field", return_value="ifName"),
        ):
            view.get_links_data(obj)
        assert view._links_fetch_error is None

    def test_prepare_context_keeps_empty_links_when_oob_fetch_failed(self):
        """An empty host-link list with a failed OOB fetch must NOT collapse to None: _prepare_context(fetch_fresh=True) must return a context so post() can show the OOB warning, instead of mislabeling it 'No links found'."""
        view = self._make_view()
        view._librenms_api.cache_timeout = 300
        obj = _mock_obj()

        def _fake_links(o, server_key=None):
            view._oob_links_fetch_failed = True  # host has no links AND the OOB fetch failed
            return []

        with (
            patch.object(view, "get_links_data", side_effect=_fake_links),
            patch.object(view, "get_cache_key", return_value="k"),
            patch.object(view, "enrich_links_data", side_effect=lambda d, *a, **k: d),
            patch.object(view, "get_table", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
        ):
            mock_cache.ttl.return_value = 300
            ctx = view._prepare_context(view.request, obj, fetch_fresh=True, server_key="default")

        assert ctx is not None
        assert view._oob_links_fetch_failed is True

    def test_prepare_context_deletes_stale_links_cache_on_partial_fetch(self):
        """A partial fresh fetch must DELETE any prior full links snapshot, not leave it cached."""
        from django.core.cache import cache as real_cache

        view = self._make_view()
        view._librenms_api.cache_timeout = 300
        obj = _mock_obj()
        cache_key = "links-partial-stale-test"
        # A prior FULL snapshot is already cached (what verify/sync would resolve rows from).
        real_cache.set(cache_key, {"links": [{"local_port": "Gi0/1", "local_port_id": 11}]})

        def _fake_links(o, server_key=None):
            view._oob_links_fetch_failed = True  # partial: the OOB-side fetch failed
            return [{"local_port": "Gi0/2", "local_port_id": 22}]  # fresh PARTIAL set

        try:
            with (
                patch.object(view, "get_links_data", side_effect=_fake_links),
                patch.object(view, "get_cache_key", return_value=cache_key),
                patch.object(view, "enrich_links_data", side_effect=lambda d, *a, **k: d),
                patch.object(view, "get_table", return_value=MagicMock()),
                patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=obj),
            ):
                view._prepare_context(view.request, obj, fetch_fresh=True, server_key="default")

            # The stale full snapshot must be gone so downstream verify/sync can't serve it.
            assert real_cache.get(cache_key) is None
        finally:
            real_cache.delete(cache_key)

    def test_get_links_data_treats_status_error_payload_as_failure(self):
        """get_device_links returns the raw JSON body, so a 200 {"status": "error", ...} must be treated as a fetch failure (with its message), not silently fall through to 'No links found'."""
        view = self._make_view()
        obj = _mock_obj()
        view._librenms_api.get_device_links.return_value = (True, {"status": "error", "message": "device unreachable"})
        with patch.object(view, "get_ports_data", return_value={"ports": []}):
            result = view.get_links_data(obj)
        assert result is None
        assert view._links_fetch_error == "device unreachable"

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

    def test_get_links_data_oob_only_device_still_renders_oob_rows(self):
        """An OOB-only sync device has no host LibreNMS id (get_librenms_id -> None), so the host links fetch is skipped and only the OOB controller is fetched."""
        view = self._make_view()
        # Clear the seeded host id so the fixture unambiguously represents the OOB-only
        # scenario (get_links_data reassigns self.librenms_id from get_librenms_id() at use,
        # but pinning it None here guards against any future read of the stale view attribute).
        view.librenms_id = None
        obj = _mock_obj()

        # Host has no LibreNMS id; only the OOB controller is mapped. Key the side_effect on the
        # device id rather than call order: the host fetch is now skipped entirely (no
        # get_device_links(None)), so only the OOB controller id (99) reaches the API.
        view._librenms_api.get_librenms_id.return_value = None

        def _links(dev_id):
            if dev_id is None:  # host fetch — must not happen now (guarded out)
                return (False, {"error": "device not found in librenms"})
            return (
                True,
                {
                    "links": [
                        {
                            "local_port_id": 5,
                            "remote_port": "eth1",
                            "remote_hostname": "peer-sw",
                            "remote_port_id": 7,
                            "remote_device_id": 88,
                        }
                    ]
                },
            )  # OOB controller (id=99) fetch succeeds

        view._librenms_api.get_device_links.side_effect = _links
        view._librenms_api.get_ports.return_value = (True, {"ports": [{"port_id": 5, "ifName": "console0"}]})

        with (
            patch.object(view, "get_ports_data", return_value={"ports": []}),
            patch("netbox_librenms_plugin.views.base.cables_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_oob", return_value={"id": 99}),
        ):
            result = view.get_links_data(obj)

        # The OOB merge runs and surfaces the OOB-side cable row even though the host has no id.
        assert result is not None
        assert len(result) == 1
        assert result[0]["_source"] == "oob"
        assert result[0]["remote_device"] == "peer-sw"
        assert result[0]["local_port"] == "console0"
        # The wasteful host link fetch is skipped: get_device_links is called exactly once, for the
        # OOB controller (99) — never with the None host id or any other value. Pin the exact arg
        # list (not merely "non-None"), since _links() treats every non-None id as the OOB success.
        assert [c.args[0] for c in view._librenms_api.get_device_links.call_args_list] == [99]

    def test_get_links_data_successful_empty_returns_list_not_none(self):
        """A successful refresh with zero rows must return [] (not None) so _prepare_context() flows it through the success path instead of mislabeling it 'No links found'."""
        view = self._make_view()
        obj = _mock_obj()
        view._librenms_api.get_device_links.return_value = (True, {"links": []})

        with (
            patch.object(view, "get_ports_data", return_value={"ports": []}),
            patch("netbox_librenms_plugin.views.base.cables_view.get_interface_name_field", return_value="ifName"),
        ):
            result = view.get_links_data(obj)

        # Pre-fix `return links_data if links_data else None` collapsed this empty success to None.
        assert result == []
        assert result is not None

    def test_get_links_data_host_success_oob_failure_empty_returns_list(self):
        """Host LLDP succeeds with zero links but the OOB controller fetch fails: must return [] (with _oob_links_fetch_failed set) so post() can surface the OOB warning — not None, which would be mislabeled 'No links found' and drop the warning."""
        view = self._make_view()
        obj = _mock_obj()
        view._librenms_api.get_device_links.side_effect = [
            (True, {"links": []}),  # host: success, no links
            (False, {"error": "oob down"}),  # OOB controller: fetch fails
        ]
        view._librenms_api.get_ports.return_value = (True, {"ports": []})

        with (
            patch.object(view, "get_ports_data", return_value={"ports": []}),
            patch("netbox_librenms_plugin.views.base.cables_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_oob", return_value={"id": 99}),
        ):
            result = view.get_links_data(obj)

        assert result == []
        assert view._oob_links_fetch_failed is True

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

    def test_get_links_data_malformed_port_rows_skipped(self):
        """Non-dict rows in the ports payload (e.g."""
        view = self._make_view()

        links_data = {"links": [{"local_port_id": 10, "remote_hostname": "sw", "remote_port": "Gi0/1"}]}
        view._librenms_api.get_device_links.return_value = (True, links_data)

        ports = {
            "ports": [
                "not-a-dict",  # malformed row — must be skipped, not .get()'d
                None,  # malformed row
                {"port_id": 10, "ifName": "Gi0/0"},  # valid row resolves
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

    def test_get_links_data_oob_malformed_port_rows_skipped(self):
        """The OOB ports loop must also skip non-dict rows so a malformed OOB ports payload cannot 500 the refresh; the valid OOB port still maps to its name."""
        view = self._make_view()

        main_links = {"links": []}
        oob_links = {
            "links": [
                {
                    "local_port_id": 99,
                    "local_port": "eth0",
                    "remote_hostname": "peer-b",
                    "remote_port": "Gi0/2",
                    "remote_port_id": 21,
                    "remote_device_id": 6,
                }
            ]
        }
        view._librenms_api.get_device_links.side_effect = [(True, main_links), (True, oob_links)]
        # OOB ports payload carries malformed rows alongside the valid one.
        view._librenms_api.get_ports.return_value = (
            True,
            {"ports": ["bogus", None, {"port_id": 99, "ifName": "Management0"}]},
        )

        main_ports = {"ports": [{"port_id": 10, "ifName": "Gi0/0"}]}
        obj = _mock_obj()

        with (
            patch.object(view, "get_ports_data", return_value=main_ports),
            patch("netbox_librenms_plugin.views.base.cables_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_oob", return_value={"id": 7}),
        ):
            result = view.get_links_data(obj)

        assert result is not None
        oob_entry = next(r for r in result if r["_source"] == "oob")
        assert oob_entry["local_port"] == "Management0"

    def test_get_links_data_oob_uses_interface_name_field(self):
        """OOB links resolve local port name via interface_name_field, not raw LLDP name."""

        view = self._make_view()

        # Main device: one direct link
        main_links = {
            "links": [
                {
                    "local_port_id": 10,
                    "remote_hostname": "peer-a",
                    "remote_port": "Gi0/1",
                    "remote_port_id": 20,
                    "remote_device_id": 5,
                }
            ]
        }
        # OOB device: one link whose raw local_port is the ifName, not the stored ifDescr
        oob_links = {
            "links": [
                {
                    "local_port_id": 99,
                    "local_port": "eth0",
                    "remote_hostname": "peer-b",
                    "remote_port": "Gi0/2",
                    "remote_port_id": 21,
                    "remote_device_id": 6,
                }
            ]
        }

        view._librenms_api.get_device_links.side_effect = [
            (True, main_links),
            (True, oob_links),
        ]
        # OOB device ports: port_id=99 maps to ifDescr "Management0" (different from raw ifName "eth0")
        view._librenms_api.get_ports.return_value = (
            True,
            {"ports": [{"port_id": 99, "ifDescr": "Management0", "ifName": "eth0"}]},
        )

        main_ports = {"ports": [{"port_id": 10, "ifDescr": "GigabitEthernet0/0"}]}
        obj = _mock_obj()

        oob_mock = {"id": 7}

        with (
            patch.object(view, "get_ports_data", return_value=main_ports),
            patch("netbox_librenms_plugin.views.base.cables_view.get_interface_name_field", return_value="ifDescr"),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_oob", return_value=oob_mock),
        ):
            result = view.get_links_data(obj)

        assert result is not None
        assert len(result) == 2
        oob_entry = next(r for r in result if r["_source"] == "oob")
        # Should use ifDescr "Management0", not raw LLDP value "eth0"
        assert oob_entry["local_port"] == "Management0"
        assert oob_entry["remote_device"] == "peer-b"
        # OOB-side fetches must target the OOB device id, not the main device.
        view._librenms_api.get_device_links.assert_any_call(oob_mock["id"])
        view._librenms_api.get_ports.assert_called_once_with(oob_mock["id"])

    def test_get_links_data_carries_alternate_name_field(self):
        """Issue #88: each link carries local_port_alt (the LibreNMS field NOT being displayed) so enrich_local_port can fall back to a NetBox interface named from the other field."""
        view = self._make_view()

        main_links = {
            "links": [
                {
                    "local_port_id": 10,
                    "remote_hostname": "peer-a",
                    "remote_port": "Gi0/1",
                    "remote_port_id": 20,
                    "remote_device_id": 5,
                }
            ]
        }
        oob_links = {
            "links": [
                {
                    "local_port_id": 99,
                    "local_port": "eth0",
                    "remote_hostname": "peer-b",
                    "remote_port": "Gi0/2",
                    "remote_port_id": 21,
                    "remote_device_id": 6,
                }
            ]
        }

        view._librenms_api.get_device_links.side_effect = [
            (True, main_links),
            (True, oob_links),
        ]
        # OOB port 99 carries both names; with ifName displayed the alt is the ifDescr.
        view._librenms_api.get_ports.return_value = (
            True,
            {"ports": [{"port_id": 99, "ifName": "eth0", "ifDescr": "Management0"}]},
        )
        # Main port 10 carries both names too.
        main_ports = {"ports": [{"port_id": 10, "ifName": "Gi0/0", "ifDescr": "GigabitEthernet0/0"}]}
        obj = _mock_obj()
        oob_mock = {"id": 7}

        with (
            patch.object(view, "get_ports_data", return_value=main_ports),
            # User is displaying ifName, so the alternate captured must be ifDescr.
            patch("netbox_librenms_plugin.views.base.cables_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_oob", return_value=oob_mock),
        ):
            result = view.get_links_data(obj)

        main_entry = next(r for r in result if r["_source"] == "main")
        assert main_entry["local_port"] == "Gi0/0"
        assert main_entry["local_port_alt"] == "GigabitEthernet0/0"

        oob_entry = next(r for r in result if r["_source"] == "oob")
        assert oob_entry["local_port"] == "eth0"
        assert oob_entry["local_port_alt"] == "Management0"

    def test_get_links_data_oob_falls_back_to_raw_name_on_port_fetch_failure(self):
        """When OOB port fetch fails, falls back to raw local_port from LLDP data."""
        view = self._make_view()

        main_links = {"links": []}
        oob_links = {
            "links": [
                {
                    "local_port_id": 99,
                    "local_port": "eth0",
                    "remote_hostname": "peer-b",
                    "remote_port": "Gi0/2",
                    "remote_port_id": 21,
                    "remote_device_id": 6,
                }
            ]
        }

        view._librenms_api.get_device_links.side_effect = [
            (True, main_links),
            (True, oob_links),
        ]
        view._librenms_api.get_ports.return_value = (False, {})

        main_ports = {"ports": []}
        obj = _mock_obj()
        oob_mock = {"id": 7}

        with (
            patch.object(view, "get_ports_data", return_value=main_ports),
            patch("netbox_librenms_plugin.views.base.cables_view.get_interface_name_field", return_value="ifDescr"),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_oob", return_value=oob_mock),
        ):
            result = view.get_links_data(obj)

        assert result is not None
        oob_entry = result[0]
        assert oob_entry["local_port"] == "eth0"  # Raw fallback
        # OOB-side fetches must target the OOB device id even on the fallback path.
        view._librenms_api.get_device_links.assert_any_call(oob_mock["id"])
        view._librenms_api.get_ports.assert_called_once_with(oob_mock["id"])

    def test_get_links_data_oob_status_error_payload_is_failure(self):
        """get_device_links returns the raw JSON body, so a 200 {"status": "error", ...} from the OOB controller must flag _oob_links_fetch_failed and append no OOB rows (the warning path in post()), not silently drop them."""
        view = self._make_view()
        view._librenms_api.get_device_links.side_effect = [
            (True, {"links": []}),  # main: no links
            (True, {"status": "error", "message": "oob unreachable"}),  # OOB: 200 error body
        ]
        obj = _mock_obj()
        oob_mock = {"id": 7}

        with (
            patch.object(view, "get_ports_data", return_value={"ports": []}),
            patch("netbox_librenms_plugin.views.base.cables_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_oob", return_value=oob_mock),
        ):
            result = view.get_links_data(obj)

        # No OOB rows appended; failure flagged for post() to warn; no OOB port fetch attempted.
        assert not any(row.get("_source") == "oob" for row in (result or []))
        assert view._oob_links_fetch_failed is True
        view._librenms_api.get_ports.assert_not_called()

    def test_get_links_data_oob_malformed_links_is_failure(self):
        """A dict OOB body whose 'links' is null/object (not a list) must flag _oob_links_fetch_failed and append no OOB rows, not crash on iteration."""
        view = self._make_view()
        view._librenms_api.get_device_links.side_effect = [
            (True, {"links": [{"local_port_id": 1, "remote_hostname": "sw1"}]}),  # main: one link
            (True, {"links": None}),  # OOB: dict body, malformed links
        ]
        obj = _mock_obj()

        with (
            patch.object(view, "get_ports_data", return_value={"ports": []}),
            patch("netbox_librenms_plugin.views.base.cables_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_oob", return_value={"id": 7}),
        ):
            view._librenms_api.get_ports.return_value = (True, {"ports": []})
            result = view.get_links_data(obj)

        # Main link preserved; OOB failure flagged; no OOB rows.
        assert any(row.get("_source") == "main" for row in (result or []))
        assert not any(row.get("_source") == "oob" for row in (result or []))
        assert view._oob_links_fetch_failed is True


@pytest.mark.django_db
class TestBaseCableTableViewGetDeviceByIdOrName:
    """Real-DB tests for BaseCableTableView.get_device_by_id_or_name."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_finds_device_by_librenms_id(self):
        """remote_device_id resolves via the librenms_id custom field, independent of name."""
        view = self._make_view()
        # Name deliberately differs from the queried hostname so only the id path can match.
        dev = make_device("real-host", librenms_cf={"default": 42})

        device, found, error = view.get_device_by_id_or_name(42, "some-other-hostname")

        assert found is True
        assert device == dev
        assert error is None

    def test_falls_back_to_name_when_no_id(self):
        """When remote_device_id is None, falls back to an exact name lookup."""
        view = self._make_view()
        dev = make_device("switch-a")

        device, found, error = view.get_device_by_id_or_name(None, "switch-a")

        assert found is True
        assert device == dev

    def test_falls_back_to_simple_hostname_when_fqdn_not_found(self):
        """When the FQDN isn't found, the short hostname (before the first dot) is tried."""
        view = self._make_view()
        dev = make_device("switch")

        device, found, error = view.get_device_by_id_or_name(None, "switch.example.com")

        assert found is True
        assert device == dev

    def test_multiple_objects_returns_error_message(self):
        """Two devices sharing a name (across sites) make the name lookup ambiguous."""
        from dcim.models import Device, Site

        view = self._make_view()
        dev1 = make_device("duplicate-switch")  # on the shared TestSite
        # A second device with the same name on a different site → Device.objects.get(name=)
        # raises MultipleObjectsReturned (the per-site uniqueness constraint doesn't span sites).
        site2 = Site.objects.create(name="Site2", slug="site2")
        Device.objects.create(
            name="duplicate-switch",
            device_type=dev1.device_type,
            role=dev1.role,
            site=site2,
            status="active",
        )

        device, found, error = view.get_device_by_id_or_name(None, "duplicate-switch")

        assert found is False
        assert device is None
        assert error is not None
        assert "duplicate-switch" in error

    def test_device_not_found_returns_none_false_none(self):
        """When no device matches by any method, returns (None, False, None)."""
        view = self._make_view()
        make_device("present-device")  # a row exists, but not the one we ask for

        device, found, error = view.get_device_by_id_or_name(None, "nonexistent")

        assert found is False
        assert device is None
        assert error is None


@pytest.mark.django_db
class TestBaseCableTableViewEnrichLocalPort:
    """Real-DB tests for BaseCableTableView.enrich_local_port."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_no_local_port_skips_enrichment(self):
        """When local_port is absent, link dict is unchanged."""
        view = self._make_view()
        obj = make_device("cable-dev-nolp")
        link = {"local_port": None}
        view.enrich_local_port(link, obj)
        assert "local_port_url" not in link

    def test_interface_found_by_librenms_id_adds_url(self):
        """The librenms_id match wins even when the local_port name differs from the iface name."""
        view = self._make_view()
        obj = make_device("cable-dev-byid")
        # Interface name deliberately != link local_port so only the librenms_id path can match.
        iface = make_interface(obj, "Ethernet1")
        iface.custom_field_data["librenms_id"] = {"default": 10}
        iface.save()

        link = {"local_port": "Gi0/0", "local_port_id": 10}
        view.enrich_local_port(link, obj)

        assert link["netbox_local_interface_id"] == iface.pk
        assert link["local_port_url"].endswith(f"/dcim/interfaces/{iface.pk}/")

    def test_interface_found_by_name_fallback(self):
        """When librenms_id match fails, falls back to name matching."""
        view = self._make_view()
        obj = make_device("cable-dev-byname")
        iface = make_interface(obj, "Gi0/1")  # no librenms_id seeded

        link = {"local_port": "Gi0/1", "local_port_id": 20}  # id 20 matches nothing
        view.enrich_local_port(link, obj)

        assert link["netbox_local_interface_id"] == iface.pk
        assert link["local_port_url"].endswith(f"/dcim/interfaces/{iface.pk}/")

    def test_name_fallback_matches_alternate_interface_name_field(self):
        """Issue #88: when the NetBox interface name matches the *non-selected* LibreNMS field (e.g."""
        view = self._make_view()
        obj = make_device("cable-dev-altname")
        iface = make_interface(obj, "GigabitEthernet0/1")  # named from ifDescr, no librenms_id

        link = {
            "local_port": "Gi0/1",  # selected field (ifName) — no NB interface by this name
            "local_port_alt": "GigabitEthernet0/1",  # other field (ifDescr) — matches the NB interface
            "local_port_id": 999,  # matches no stored librenms_id
        }
        view.enrich_local_port(link, obj)

        assert link["netbox_local_interface_id"] == iface.pk
        assert link["local_port_url"].endswith(f"/dcim/interfaces/{iface.pk}/")

    def test_no_interface_found_leaves_link_unchanged(self):
        """When no interface matches by id or name, link dict is not modified."""
        view = self._make_view()
        obj = make_device("cable-dev-nomatch")
        make_interface(obj, "Gi9/9/9")  # present but not the one referenced

        link = {"local_port": "Gi0/2", "local_port_id": 30}
        view.enrich_local_port(link, obj)

        assert "local_port_url" not in link

    def test_virtual_chassis_delegates_to_chassis_member(self):
        """With a VC, the interface is resolved on the member matching the port's slot number."""
        from dcim.models import VirtualChassis

        view = self._make_view()
        vc = VirtualChassis.objects.create(name="vc-enrich")
        master = make_device("vc-enrich-master")
        master.virtual_chassis = vc
        master.vc_position = 9
        master.save()
        member1 = make_device("vc-enrich-member1")
        member1.virtual_chassis = vc
        member1.vc_position = 1
        member1.save()
        # "Gi1/0/1" → get_virtual_chassis_member picks vc_position=1 (member1).
        iface = make_interface(member1, "Gi1/0/1")

        link = {"local_port": "Gi1/0/1", "local_port_id": 100}
        view.enrich_local_port(link, master)

        assert link["netbox_local_interface_id"] == iface.pk
        assert link["local_port_url"].endswith(f"/dcim/interfaces/{iface.pk}/")


class TestBaseCableTableViewCheckCableStatus:
    """Tests for BaseCableTableView.check_cable_status."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        return view

    @pytest.mark.django_db
    def test_cable_found_sets_cable_url(self):
        """A real cable between the two interfaces → cable_status='Cable Found' + cable_url."""
        from netbox_librenms_plugin.tests.conftest import cable_together

        view = self._make_view()
        dev = make_device("cable-status-dev")
        local_iface = make_interface(dev, "Gi0/0")
        remote_iface = make_interface(dev, "Gi0/1")
        cable = cable_together(local_iface, remote_iface)

        link = {"netbox_local_interface_id": local_iface.pk, "netbox_remote_interface_id": remote_iface.pk}
        result = view.check_cable_status(link)

        assert result["cable_status"] == "Cable Found"
        assert result["cable_url"].endswith(f"/dcim/cables/{cable.pk}/")
        assert result["can_create_cable"] is False

    @pytest.mark.django_db
    def test_no_cable_sets_can_create_cable(self):
        """Two real, uncabled interfaces → cable_status='No Cable' and can_create_cable=True."""
        view = self._make_view()
        dev = make_device("cable-status-dev2")
        local_iface = make_interface(dev, "Gi1/0")
        remote_iface = make_interface(dev, "Gi1/1")

        link = {"netbox_local_interface_id": local_iface.pk, "netbox_remote_interface_id": remote_iface.pk}
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

    def test_non_default_server_key_forwarded_to_enrich(self):
        """When librenms_api.server_key is non-default, _prepare_context passes it to enrich_links_data."""
        view = self._make_view()
        view._librenms_api.server_key = "non-default"
        obj = _mock_obj()

        links = [
            {
                "local_port": "Gi0/0",
                "local_port_id": "1",
                "remote_device": None,
                "remote_port": None,
                "remote_port_id": None,
                "remote_device_id": None,
            }
        ]
        mock_table = MagicMock()
        mock_table.configure = MagicMock()

        with (
            patch.object(view, "get_links_data", return_value=links),
            patch.object(view, "enrich_links_data", return_value=links) as mock_enrich,
            patch.object(view, "get_cache_key", return_value="cable-key"),
            patch.object(view, "get_table", return_value=mock_table),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=None),
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.cables_view.timezone") as mock_tz,
        ):
            mock_cache.ttl.return_value = 300
            mock_tz.now.return_value = MagicMock()
            mock_tz.timedelta.return_value = MagicMock()
            result = view._prepare_context(view.request, obj, fetch_fresh=True)

        assert result is not None
        # enrich_links_data must be called with the non-default server_key
        mock_enrich.assert_called_once()
        _, enrich_kwargs = mock_enrich.call_args
        assert enrich_kwargs.get("server_key") == "non-default"


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

    def test_post_rebinds_api_to_posted_server_before_fetch(self):
        """The POST must rebind the API to the posted server BEFORE get_librenms_id/get_ports, so live fetches and cache writes target the same server (no cross-server cache write)."""
        from unittest.mock import patch

        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        view = object.__new__(BaseInterfaceTableView)
        obj = MagicMock(pk=1)
        view.get_object = MagicMock(return_value=obj)
        # Use the real librenms_api property (reads self._librenms_api) so rebind_api_for_server
        # can actually swap the client post() uses — otherwise the test can't tell whether the
        # rebind took effect or "prod" was merely threaded through bookkeeping.
        session_api = MagicMock(server_key="default")
        view._librenms_api = session_api
        rebound_api = MagicMock(server_key="prod")
        rebound_api.get_librenms_id.return_value = None  # short-circuit after rebind

        req = MagicMock()
        req.POST.get.side_effect = lambda k, d=None: {"server_key": "prod"}.get(k, d)

        with (
            patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=rebound_api) as mock_build,
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="name"),
            # The id lookup now runs on the resolved VC sync device; pin it to obj so the
            # assertion stays focused on which API client (rebound vs session) is used.
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.redirect", return_value="redir") as mock_redirect,
        ):
            view.get_redirect_url = MagicMock(return_value="/back/")
            result = view.post(req, pk=1)

        # Rebind happens with the posted key, and the id lookup runs on the REBOUND client,
        # never the session one — proving the rebind swapped the client post() uses.
        mock_build.assert_called_once_with("prod")
        rebound_api.get_librenms_id.assert_called_once_with(obj)
        session_api.get_librenms_id.assert_not_called()
        assert result == "redir"
        mock_redirect.assert_called_once()

    def test_post_stale_server_key_redirects(self):
        """A posted server_key that no longer resolves (build returns None) → error + redirect, not an unhandled 500."""
        from unittest.mock import patch

        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        view = object.__new__(BaseInterfaceTableView)
        obj = MagicMock(pk=1)
        view.get_object = MagicMock(return_value=obj)
        # Real property again, so a None rebind result leaves self._librenms_api untouched
        # and we can assert the session client was never queried.
        session_api = MagicMock(server_key="default")
        view._librenms_api = session_api

        req = MagicMock()
        req.POST.get.side_effect = lambda k, d=None: {"server_key": "ghost"}.get(k, d)

        with (
            patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="name"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.base.interfaces_view.redirect", return_value="redir") as mock_redirect,
        ):
            view.get_redirect_url = MagicMock(return_value="/back/")
            result = view.post(req, pk=1)

        # Never reached the live id lookup; surfaced an error and redirected.
        session_api.get_librenms_id.assert_not_called()
        mock_messages.error.assert_called_once()
        assert result == "redir"
        mock_redirect.assert_called_once()

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
        """When librenms_id not found, error message and redirect — and the stale ports snapshot is cleared FIRST, so a failed refresh on a previously-synced device can't leave old interface data for the redirected tab or downstream sync to consume."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        view._librenms_api.get_librenms_id.return_value = None

        with (
            patch.object(view, "get_object", return_value=obj),
            patch.object(view, "get_redirect_url", return_value="/device/1/"),
            patch.object(view, "rebind_api_for_server", return_value="prod"),
            patch.object(view, "get_cache_key", return_value="cache-key"),
            patch.object(view, "get_last_fetched_key", return_value="last-key"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.base.interfaces_view.redirect") as mock_redirect,
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
        ):
            mock_redirect.return_value = MagicMock()
            view.post(request, pk=1)

        mock_messages.error.assert_called_once()
        # The failure redirect must preserve the POST-scoped server_key so the user stays on
        # the same LibreNMS server for the next retry.
        mock_redirect.assert_called_once_with("/device/1/?server_key=prod")
        # The snapshot invalidation must run even on the missing-librenms_id path (it precedes
        # the early return); otherwise a prior successful snapshot survives a failed refresh.
        mock_cache.delete.assert_any_call("cache-key")
        mock_cache.delete.assert_any_call("last-key")
        mock_cache.set.assert_not_called()

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
            patch.object(view, "rebind_api_for_server", return_value="prod"),
            patch.object(view, "get_cache_key", return_value="cache-key"),
            patch.object(view, "get_last_fetched_key", return_value="last-key"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.base.interfaces_view.redirect") as mock_redirect,
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
        ):
            mock_redirect.return_value = MagicMock()
            view.post(request, pk=1)

        mock_messages.error.assert_called_once_with(request, "Connection refused")
        # Failure redirect preserves the POST-scoped server_key (see _failure_redirect).
        mock_redirect.assert_called_once_with("/device/1/?server_key=prod")
        # The stale snapshot is cleared up front; a failed fetch must not re-populate it,
        # so the next render shows an empty view rather than old data.
        mock_cache.delete.assert_any_call("cache-key")
        mock_cache.delete.assert_any_call("last-key")
        mock_cache.set.assert_not_called()

    def test_post_malformed_main_ports_payload_treated_as_failure(self):
        """A truthy success with a malformed MAIN ports payload (ports not a list of dicts) must fail closed — warn + failure-redirect, no degraded snapshot cached — mirroring the OOB branch."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.get_ports.return_value = (True, {"ports": "not-a-list"})

        with (
            patch.object(view, "get_object", return_value=obj),
            patch.object(view, "get_redirect_url", return_value="/device/1/"),
            patch.object(view, "rebind_api_for_server", return_value="prod"),
            patch.object(view, "get_cache_key", return_value="cache-key"),
            patch.object(view, "get_last_fetched_key", return_value="last-key"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.base.interfaces_view.redirect") as mock_redirect,
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
        ):
            mock_redirect.return_value = MagicMock()
            view.post(request, pk=1)

        mock_messages.error.assert_called_once_with(
            request, "Unexpected response from LibreNMS (malformed ports payload)."
        )
        mock_redirect.assert_called_once_with("/device/1/?server_key=prod")
        mock_cache.set.assert_not_called()

    def test_post_malformed_main_ports_non_dict_row_treated_as_failure(self):
        """The docstring sibling above only covers a non-list ports payload."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.get_ports.return_value = (True, {"ports": [42]})

        with (
            patch.object(view, "get_object", return_value=obj),
            patch.object(view, "get_redirect_url", return_value="/device/1/"),
            patch.object(view, "rebind_api_for_server", return_value="prod"),
            patch.object(view, "get_cache_key", return_value="cache-key"),
            patch.object(view, "get_last_fetched_key", return_value="last-key"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.base.interfaces_view.redirect") as mock_redirect,
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
        ):
            mock_redirect.return_value = MagicMock()
            view.post(request, pk=1)

        mock_messages.error.assert_called_once_with(
            request, "Unexpected response from LibreNMS (malformed ports payload)."
        )
        mock_redirect.assert_called_once_with("/device/1/?server_key=prod")
        mock_cache.set.assert_not_called()

    def test_failure_redirect_gated_by_open_redirect_barrier(self):
        """The appended server_key is POST-derived, so the failure redirect MUST gate the candidate URL through url_has_allowed_host_and_scheme (the CodeQL py/url-redirection barrier)."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()
        # Seed the POSTed server_key so the test proves post() actually reads the submitted key:
        # without it, request.POST is empty and rebind returns "prod" for any input, so a
        # regression that stopped reading the key would still pass.
        request.POST = {"server_key": "prod"}
        view._librenms_api.get_librenms_id.return_value = None

        with (
            patch.object(view, "get_object", return_value=obj),
            patch.object(view, "get_redirect_url", return_value="/device/1/"),
            patch.object(view, "rebind_api_for_server", return_value="prod") as mock_rebind,
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            # Pin the VC sync-device resolution to obj so lookup_device is deterministic
            # (mirrors every sibling post() test); without it line 161 runs the real
            # get_librenms_sync_device against a bare MagicMock and lookup_device becomes a
            # fabricated mock rather than obj — the test would then pass only by accident.
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages"),
            patch(
                "netbox_librenms_plugin.views.base.interfaces_view.url_has_allowed_host_and_scheme",
                return_value=False,
            ) as mock_barrier,
            patch("netbox_librenms_plugin.views.base.interfaces_view.redirect") as mock_redirect,
        ):
            mock_redirect.return_value = MagicMock()
            view.post(request, pk=1)

        # post() must read the POSTed server_key (proving the redirect candidate really is
        # POST-derived), then the barrier must be consulted and the tainted key dropped on reject.
        mock_rebind.assert_called_once_with("prod")
        mock_barrier.assert_called_once()
        # Pin the barrier INPUT, not just that it was called: the candidate URL gated through
        # url_has_allowed_host_and_scheme must be the one carrying the POST-derived server_key.
        # Otherwise a regression that validated the bare "/device/1/" and appended the key after
        # the check would still pass this test while reintroducing the open-redirect path.
        assert mock_barrier.call_args.args[0] == "/device/1/?server_key=prod"
        mock_redirect.assert_called_once_with("/device/1/")

    def test_post_clears_stale_cache_before_fetch(self):
        """A refresh drops the previous ports snapshot before fetching, so a later failure can't leave stale data behind."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        view._librenms_api.get_librenms_id.return_value = 42
        view._librenms_api.get_ports.return_value = (True, {"ports": [{"port_id": 1, "ifName": "Gi0/0"}]})

        delete_calls_before_get_ports = []

        def _record_get_ports(_id):
            delete_calls_before_get_ports.extend(mock_cache.delete.call_args_list)
            return (True, {"ports": [{"port_id": 1, "ifName": "Gi0/0"}]})

        with (
            patch.object(view, "get_object", return_value=obj),
            patch.object(view, "get_redirect_url", return_value="/device/1/"),
            patch.object(view, "_enrich_ports_with_vlan_data", return_value=[]),
            patch.object(view, "get_context_data", return_value={}),
            patch.object(view, "get_cache_key", return_value="cache-key"),
            patch.object(view, "get_last_fetched_key", return_value="last-key"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.render") as mock_render,
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.interfaces_view.timezone"),
        ):
            view._librenms_api.get_ports.side_effect = _record_get_ports
            mock_render.return_value = MagicMock()
            view.post(request, pk=1)

        # The cache was cleared (both keys) before the LibreNMS fetch ran.
        recorded = [c.args[0] for c in delete_calls_before_get_ports]
        assert "cache-key" in recorded
        assert "last-key" in recorded
        # Success still re-populates the cache afterwards.
        mock_cache.set.assert_called()

    def test_post_oob_fetch_failure_caches_incomplete_snapshot(self):
        """When the linked OOB controller's ports fetch fails, the host-only snapshot is cached tagged oob_incomplete (not deleted) so downstream verify/apply keep a backing snapshot and the incomplete state can be surfaced."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        view._librenms_api.get_librenms_id.return_value = 42
        # Host ports OK; OOB controller ports fetch fails.
        view._librenms_api.get_ports.side_effect = [
            (True, {"ports": [{"port_id": 1, "ifName": "Gi0/0"}]}),
            (False, "boom"),
        ]

        with (
            patch.object(view, "get_object", return_value=obj),
            patch.object(view, "get_redirect_url", return_value="/device/1/"),
            patch.object(view, "_enrich_ports_with_vlan_data", side_effect=lambda ports, field: ports),
            patch.object(view, "get_context_data", return_value={}),
            patch.object(view, "get_cache_key", return_value="cache-key"),
            patch.object(view, "get_last_fetched_key", return_value="last-key"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_oob", return_value={"id": 99}),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.base.interfaces_view.render") as mock_render,
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.interfaces_view.timezone"),
        ):
            mock_render.return_value = MagicMock()
            view.post(request, pk=1)

        # The host-only snapshot is cached (not deleted) and tagged incomplete.
        ports_set_calls = [c for c in mock_cache.set.call_args_list if c.args and c.args[0] == "cache-key"]
        assert len(ports_set_calls) == 1
        cached_snapshot = ports_set_calls[0].args[1]
        assert cached_snapshot["oob_incomplete"] is True
        # The OOB-fetch failure is surfaced to the user.
        mock_messages.warning.assert_called()

    def test_post_oob_malformed_but_successful_payload_treated_as_incomplete(self):
        """get_ports is an external boundary: oob_success=True does not guarantee a dict with a list of dict rows."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        view._librenms_api.get_librenms_id.return_value = 42
        # Host ports OK; OOB controller returns success but a malformed payload (non-dict row).
        view._librenms_api.get_ports.side_effect = [
            (True, {"ports": [{"port_id": 1, "ifName": "Gi0/0"}]}),
            (True, {"ports": [42]}),
        ]

        with (
            patch.object(view, "get_object", return_value=obj),
            patch.object(view, "get_redirect_url", return_value="/device/1/"),
            patch.object(view, "_enrich_ports_with_vlan_data", side_effect=lambda ports, field: ports),
            # create=True: downstream branches add a _has_lag_signals() port_stack probe (DB-backed
            # PortStackLagPattern). Short-circuit it so this non-DB test stays isolated; harmless
            # where the method doesn't exist yet.
            patch.object(view, "_has_lag_signals", return_value=False, create=True),
            patch.object(view, "get_context_data", return_value={}),
            patch.object(view, "get_cache_key", return_value="cache-key"),
            patch.object(view, "get_last_fetched_key", return_value="last-key"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_oob", return_value={"id": 99}),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.base.interfaces_view.render") as mock_render,
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.interfaces_view.timezone"),
        ):
            mock_render.return_value = MagicMock()
            view.post(request, pk=1)  # must not raise

        ports_set_calls = [c for c in mock_cache.set.call_args_list if c.args and c.args[0] == "cache-key"]
        assert len(ports_set_calls) == 1
        assert ports_set_calls[0].args[1]["oob_incomplete"] is True
        mock_messages.warning.assert_called()

    def test_post_oob_non_string_mac_does_not_crash(self):
        """get_ports is an external boundary: a malformed truthy ifPhysAddress (int/list) on a host or OOB port must be treated as absent in the shared-LOM dedup, not 500 on .lower() after the cache was already cleared."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        view._librenms_api.get_librenms_id.return_value = 42
        # Host port carries a corrupt non-string MAC; the OOB controller exposes a valid one.
        # The enricher is passed through unchanged, so the real dedup block runs on these dicts.
        view._librenms_api.get_ports.side_effect = [
            (True, {"ports": [{"port_id": 1, "ifName": "Gi0/0", "ifPhysAddress": 123}]}),
            (True, {"ports": [{"port_id": 2, "ifName": "MGMT", "ifPhysAddress": ["aa:bb"]}]}),
        ]

        with (
            patch.object(view, "get_object", return_value=obj),
            patch.object(view, "get_redirect_url", return_value="/device/1/"),
            patch.object(view, "_enrich_ports_with_vlan_data", side_effect=lambda ports, field: ports),
            patch.object(view, "_has_lag_signals", return_value=False, create=True),
            patch.object(view, "get_context_data", return_value={}),
            patch.object(view, "get_cache_key", return_value="cache-key"),
            patch.object(view, "get_last_fetched_key", return_value="last-key"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_oob", return_value={"id": 99}),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.render") as mock_render,
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.interfaces_view.timezone"),
        ):
            mock_render.return_value = MagicMock()
            view.post(request, pk=1)  # must not raise on the non-string MACs

        ports_set_calls = [c for c in mock_cache.set.call_args_list if c.args and c.args[0] == "cache-key"]
        assert len(ports_set_calls) == 1
        snapshot = ports_set_calls[0].args[1]
        # Both rows merged; the corrupt MACs were treated as absent (no shared-LOM conflict).
        assert any(p.get("_source") == "oob" for p in snapshot["ports"])
        assert not any(p.get("_dedup_conflict") for p in snapshot["ports"])

    def test_post_placeholder_mac_not_flagged_as_shared_lom(self):
        """A placeholder 00:00:00:00:00:00 on both host and OOB is not a real address, so it must not flag a shared-LOM conflict — while a genuinely shared MAC still does."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        view._librenms_api.get_librenms_id.return_value = 42
        # Host + OOB each carry a placeholder MAC (must NOT conflict) and a genuinely shared
        # real MAC (must still conflict). Colon vs hyphen formatting differs to prove normalization.
        view._librenms_api.get_ports.side_effect = [
            (
                True,
                {
                    "ports": [
                        {"port_id": 1, "ifName": "Gi0/0", "ifPhysAddress": "00:00:00:00:00:00"},
                        {"port_id": 2, "ifName": "Gi0/1", "ifPhysAddress": "aa:bb:cc:dd:ee:ff"},
                    ]
                },
            ),
            (
                True,
                {
                    "ports": [
                        {"port_id": 3, "ifName": "MGMT", "ifPhysAddress": "00:00:00:00:00:00"},
                        {"port_id": 4, "ifName": "LOM", "ifPhysAddress": "AA-BB-CC-DD-EE-FF"},
                    ]
                },
            ),
        ]

        with (
            patch.object(view, "get_object", return_value=obj),
            patch.object(view, "get_redirect_url", return_value="/device/1/"),
            patch.object(view, "_enrich_ports_with_vlan_data", side_effect=lambda ports, field: ports),
            patch.object(view, "_has_lag_signals", return_value=False, create=True),
            patch.object(view, "get_context_data", return_value={}),
            patch.object(view, "get_cache_key", return_value="cache-key"),
            patch.object(view, "get_last_fetched_key", return_value="last-key"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_oob", return_value={"id": 99}),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.render") as mock_render,
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.interfaces_view.timezone"),
        ):
            mock_render.return_value = MagicMock()
            view.post(request, pk=1)

        ports_set_calls = [c for c in mock_cache.set.call_args_list if c.args and c.args[0] == "cache-key"]
        assert len(ports_set_calls) == 1
        by_id = {p["port_id"]: p for p in ports_set_calls[0].args[1]["ports"]}
        # Placeholder-MAC ports are not flagged...
        assert not by_id[1].get("_dedup_conflict")
        assert not by_id[3].get("_dedup_conflict")
        # ...but the genuinely shared real MAC still is (non-vacuous: dedup still works).
        assert by_id[2].get("_dedup_conflict")
        assert by_id[4].get("_dedup_conflict")

    def test_post_oob_ports_fetch_failure_warning_omits_oob_id(self):
        """The user-facing OOB-fetch-failure toast must not leak the internal LibreNMS OOB id (it is logged server-side only)."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        view._librenms_api.get_librenms_id.return_value = 42
        # Host ports OK; OOB controller fetch fails outright.
        view._librenms_api.get_ports.side_effect = [
            (True, {"ports": [{"port_id": 1, "ifName": "Gi0/0"}]}),
            (False, "boom"),
        ]

        with (
            patch.object(view, "get_object", return_value=obj),
            patch.object(view, "get_redirect_url", return_value="/device/1/"),
            patch.object(view, "_enrich_ports_with_vlan_data", side_effect=lambda ports, field: ports),
            patch.object(view, "_has_lag_signals", return_value=False, create=True),
            patch.object(view, "get_context_data", return_value={}),
            patch.object(view, "get_cache_key", return_value="cache-key"),
            patch.object(view, "get_last_fetched_key", return_value="last-key"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_oob", return_value={"id": 99}),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.base.interfaces_view.render") as mock_render,
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.timezone"),
        ):
            mock_render.return_value = MagicMock()
            view.post(request, pk=1)

        mock_messages.warning.assert_called()
        # Inspect only the string message args — joining the request MagicMock's repr would
        # spuriously match digits from its object id, so the test must assert on the real message.
        warning_msgs = [a for call in mock_messages.warning.call_args_list for a in call.args if isinstance(a, str)]
        oob_msg = next((m for m in warning_msgs if "OOB controller ports fetch failed" in m), None)
        assert oob_msg is not None  # the OOB-fetch failure is surfaced to the user
        assert "99" not in oob_msg  # but the internal OOB id stays out of the UI

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
            # Ports cache scopes to the VC sync device; pin it to obj so this caching test
            # isn't entangled with VC-routing (cache_device == obj for non-VC anyway).
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_sync_device", return_value=obj),
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


@pytest.mark.django_db
class TestBaseInterfaceTablePostCoercesLibreNMSId:
    """post() must coerce whatever get_librenms_id() hands back before trusting it.

    get_librenms_id() resolves through three paths — the librenms_id custom field, the
    device-id cache, and live API discovery. The custom-field and discovery paths already
    coerce (reject bool/zero/non-numeric), but the *cache* path returns its value verbatim.
    A poisoned cache holding ``True`` therefore reaches the view as a truthy non-int that
    ``int(True)`` would silently turn into device id ``1`` — fetching a stranger's ports.
    The view must fail closed on it BEFORE get_ports().
    """

    def _real_api(self):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI

        with patch(
            "netbox_librenms_plugin.librenms_api.get_plugin_config",
            return_value={
                "default": {
                    "librenms_url": "https://lnms.example.com",
                    "api_token": "tok",
                    "cache_timeout": 300,
                    "verify_ssl": True,
                }
            },
        ):
            return LibreNMSAPI(server_key="default")

    def test_corrupt_cached_id_fails_closed_before_get_ports(self):
        """A boolean cached under the device-id key must fail closed at the view, never get_ports()."""
        from django.core.cache import cache as real_cache
        from django.test import RequestFactory

        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = make_device("coerce-id-host")  # no librenms_id custom field → forces the cache path
        api = self._real_api()

        # Poison the device-id cache. This is the ONE get_librenms_id path that does not coerce,
        # so it is the realistic vector for a non-int id reaching the view.
        real_cache.set(api._get_cache_key(device), True)
        try:
            # The real lookup really does hand back the uncoerced bool — proving this isn't a
            # straw-man mock return; if get_librenms_id ever starts coercing this path the
            # premise (and this test) should be revisited.
            assert api.get_librenms_id(device) is True

            view = object.__new__(DeviceInterfaceTableView)
            view._librenms_api = api
            # get_ports is the external HTTP boundary; spy on it to prove it is never reached.
            # The return value is irrelevant on the (correct) fail-closed path — it only matters
            # for the unfixed regression, where get_ports IS called and this failure tuple keeps
            # the red failure on a clean assertion rather than a downstream render error.
            api.get_ports = MagicMock(name="get_ports", return_value=(False, "should-not-be-called"))

            request = RequestFactory().post(f"/plugins/librenms/devices/{device.pk}/interface-sync/", data={})
            view.request = request

            with patch("netbox_librenms_plugin.views.base.interfaces_view.messages") as mock_messages:
                response = view.post(request, pk=device.pk)
        finally:
            real_cache.delete(api._get_cache_key(device))

        # Coerced to None → fail closed: error surfaced, redirect issued, get_ports never called.
        assert view.librenms_id is None
        api.get_ports.assert_not_called()
        mock_messages.error.assert_called_once_with(request, "Device not found in LibreNMS.")
        assert response.status_code == 302


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

    def test_malformed_non_dict_cache_degrades_to_empty_table(self):
        """A truthy but non-dict ports cache entry (legacy/older-shape or corrupt) must degrade to an empty table, not AttributeError-500 on .get('ports')."""
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
            # Truthy but wrong shape (a stale list-shaped snapshot). Unfixed `if cached_data:` calls
            # .get("ports") on the list → AttributeError-500. Fixed isinstance(dict) guard skips it.
            mock_cache.get.side_effect = lambda key: [{"ports": []}] if key == "key" else None
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
        mock_ifaces_qs.select_related.return_value.prefetch_related.return_value = [mock_iface]

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
        # A complete snapshot is not flagged incomplete.
        assert ctx["oob_incomplete"] is False

    def test_get_context_data_survives_cache_backend_without_ttl(self):
        """cache.ttl() is Redis-specific and not part of the Django cache API."""
        view = self._make_view()
        obj = _mock_obj()
        obj.virtual_chassis = None
        request = _mock_request()

        cached_data = {"ports": [{"port_id": 1, "ifName": "Gi0/0", "ifAdminStatus": "up", "ifDescr": "Gi0/0"}]}

        mock_iface = MagicMock()
        mock_iface.name = "Gi0/0"
        mock_ifaces_qs = MagicMock()
        mock_ifaces_qs.select_related.return_value = [mock_iface]
        mock_table = MagicMock()

        # spec without "ttl" → getattr(cache, "ttl", ...) must take the fallback; a direct
        # cache.ttl() call (the pre-fix code) would raise AttributeError here.
        mock_cache = MagicMock(spec=["get", "set", "delete"])
        mock_cache.get.side_effect = lambda key: cached_data if key == "key" else None

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
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache", mock_cache),
        ):
            ctx = view.get_context_data(request, obj, "ifName")  # must not raise

        assert ctx["table"] is mock_table
        assert ctx["cache_expiry"] is None

    def test_oob_incomplete_flag_surfaced_from_cache(self):
        """A cached snapshot tagged oob_incomplete surfaces the flag in context so the template can warn that OOB rows are missing."""
        view = self._make_view()
        obj = _mock_obj()
        obj.virtual_chassis = None
        request = _mock_request()

        cached_data = {
            "ports": [{"port_id": 1, "ifName": "Gi0/0", "ifAdminStatus": "up", "ifAlias": None, "ifDescr": "Gi0/0"}],
            "oob_incomplete": True,
        }

        mock_iface = MagicMock()
        mock_iface.name = "Gi0/0"
        mock_ifaces_qs = MagicMock()
        mock_ifaces_qs.select_related.return_value = [mock_iface]

        with (
            patch.object(view, "get_cache_key", return_value="key"),
            patch.object(view, "get_last_fetched_key", return_value="last-key"),
            patch.object(view, "get_vlan_overrides_key", return_value="overrides-key"),
            patch.object(view, "get_vlan_groups_for_device", return_value=[]),
            patch.object(view, "_build_vlan_lookup_maps", return_value={"vid_to_groups": {}, "vid_to_vlans": {}}),
            patch.object(view, "get_interfaces", return_value=mock_ifaces_qs),
            patch.object(view, "_add_vlan_group_selection"),
            patch.object(view, "_add_missing_vlans_info"),
            patch.object(view, "get_table", return_value=MagicMock()),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.interfaces_view.timezone") as mock_tz,
        ):
            mock_cache.get.side_effect = lambda key: cached_data if key == "key" else None
            mock_cache.ttl.return_value = 300
            mock_tz.now.return_value = MagicMock()
            mock_tz.timedelta.return_value = MagicMock()
            ctx = view.get_context_data(request, obj, "ifName")

        assert ctx["oob_incomplete"] is True

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
        mock_iface_qs.select_related.return_value.prefetch_related.return_value = []

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

    def test_cache_hit_non_vc_ignores_duplicate_librenms_ids(self):
        """Conflicting interface librenms_id values must not create an arbitrary port-id match."""
        view = self._make_view()
        obj = _mock_obj()
        obj.virtual_chassis = None
        obj.id = 1
        request = _mock_request()

        cached_data = {
            "ports": [
                {
                    "port_id": 101,
                    "ifName": "Gi0/99",
                    "ifAdminStatus": "up",
                    "ifAlias": None,
                    "ifDescr": "Gi0/99",
                }
            ]
        }

        interface_a = MagicMock()
        interface_a.id = 10
        interface_a.name = "Gi0/0"
        interface_b = MagicMock()
        interface_b.id = 11
        interface_b.name = "Gi0/1"

        mock_ifaces_qs = MagicMock()
        mock_ifaces_qs.select_related.return_value.prefetch_related.return_value = [interface_a, interface_b]

        rows_store = {}

        def capture_table(rows, *_args, **_kwargs):
            rows_store["rows"] = rows
            table = MagicMock()
            table.configure = MagicMock()
            return table

        with (
            patch.object(view, "get_cache_key", return_value="key"),
            patch.object(view, "get_last_fetched_key", return_value="last-key"),
            patch.object(view, "get_vlan_overrides_key", return_value="overrides-key"),
            patch.object(view, "get_vlan_groups_for_device", return_value=[]),
            patch.object(view, "_build_vlan_lookup_maps", return_value={"vid_to_groups": {}, "vid_to_vlans": {}}),
            patch.object(view, "get_interfaces", return_value=mock_ifaces_qs),
            patch.object(view, "_add_vlan_group_selection"),
            patch.object(view, "_add_missing_vlans_info"),
            patch.object(view, "get_table", side_effect=capture_table),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.interfaces_view.timezone") as mock_tz,
        ):
            view._librenms_api.get_stored_librenms_id.side_effect = lambda interface: 101
            mock_cache.get.side_effect = lambda key: cached_data if key == "key" else None
            mock_cache.ttl.return_value = 300
            mock_tz.now.return_value = MagicMock()
            mock_tz.timedelta.return_value = MagicMock()
            view.get_context_data(request, obj, "ifName")

        assert "rows" in rows_store
        assert len(rows_store["rows"]) == 1
        assert rows_store["rows"][0]["exists_in_netbox"] is False
        assert rows_store["rows"][0]["netbox_interface"] is None
        assert view._librenms_api.get_stored_librenms_id.call_count == 2
        view._librenms_api.get_librenms_id.assert_not_called()


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

    @pytest.mark.django_db
    def test_vlan_group_overrides_applied(self):
        """vlan_group_overrides replace auto-selection via a real VLANGroup.objects.in_bulk lookup."""
        from ipam.models import VLANGroup

        view = self._make_view()
        override_group = VLANGroup.objects.create(name="Override-Group", slug="override-group")
        port = {"untagged_vlan": 100, "tagged_vlans": []}
        lookup_maps = {"vid_to_groups": {}}
        device = make_device("vlan-ovr-dev")

        view._add_vlan_group_selection(port, lookup_maps, device, vlan_group_overrides={"100": str(override_group.pk)})

        assert port["vlan_group_map"][100]["group_id"] == str(override_group.pk)
        assert port["vlan_group_map"][100]["group_name"] == "Override-Group"

    @pytest.mark.django_db
    def test_override_with_empty_string_forces_global(self):
        """Override with empty string means 'No Group (Global)' (real in_bulk returns nothing)."""
        view = self._make_view()
        port = {"untagged_vlan": 100, "tagged_vlans": []}
        lookup_maps = {"vid_to_groups": {}}
        device = make_device("vlan-ovr-dev2")

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


@pytest.mark.django_db
class TestBaseIPAddressTableViewPrefetchNetboxData:
    """Real-DB tests for BaseIPAddressTableView._prefetch_netbox_data."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        view = object.__new__(BaseIPAddressTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_builds_lookup_maps(self):
        """_prefetch_netbox_data builds interface (by id + name) and IP lookup maps."""
        view = self._make_view()
        obj = make_device("prefetch-dev")
        iface = make_interface(obj, "Gi0/0")
        iface.custom_field_data["librenms_id"] = {"default": 10}
        iface.save()
        ip = make_ip("198.51.100.5/24", assigned_object=iface)

        result = view._prefetch_netbox_data(obj)

        assert result["interfaces_by_name"]["Gi0/0"] == iface
        # Per-server id scoping: the librenms_id CF resolves under server_key "default".
        assert result["interfaces_by_librenms_id"]["10"] == iface
        assert str(ip.address) in result["ip_addresses_map"]
        assert result["device"] is obj

    def test_duplicate_librenms_id_is_dropped_from_map(self):
        """Two interfaces sharing a server-scoped librenms_id are ambiguous — the id must be dropped from the lookup map so IP binding falls back to (unambiguous) name matching instead of binding to whichever interface was iterated last."""
        view = self._make_view()
        obj = make_device("prefetch-dup-dev")
        a = make_interface(obj, "Gi0/1")
        a.custom_field_data["librenms_id"] = {"default": 20}
        a.save()
        b = make_interface(obj, "Gi0/2")
        b.custom_field_data["librenms_id"] = {"default": 20}  # same id → ambiguous
        b.save()

        result = view._prefetch_netbox_data(obj)

        assert "20" not in result["interfaces_by_librenms_id"]
        # Names are still unambiguous and remain usable for the fallback match.
        assert result["interfaces_by_name"]["Gi0/1"] == a
        assert result["interfaces_by_name"]["Gi0/2"] == b


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
        mock_ifaces_qs.select_related.return_value.prefetch_related.return_value = [mock_iface]

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
        mock_ifaces_qs.select_related.return_value.prefetch_related.return_value = [netbox_only_iface]

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
        mock_ifaces_qs.select_related.return_value.prefetch_related.return_value = [netbox_only_iface]

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


# =============================================================================
# BaseIPAddressTableView._flag_management_ip
# =============================================================================


class TestBaseIPAddressTableViewFlagManagementIp:
    """Tests for marking the LibreNMS management-IP row (Set Primary IP support)."""

    def _make_view(self, librenms_id=42):
        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        view = object.__new__(BaseIPAddressTableView)
        view.librenms_id = librenms_id
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    # ── _flag_management_ip: pure flagging from a resolved mgmt IP (no API call) ──

    def test_flags_matching_entry(self):
        view = self._make_view()
        data = [{"ip_address": "10.0.0.5"}, {"ip_address": "10.0.0.1"}]
        view._flag_management_ip(data, "10.0.0.1")
        assert data[0].get("is_mgmt_ip") is None
        assert data[1].get("is_mgmt_ip") is True
        # Flagging must never hit the API — the mgmt IP is resolved/cached upstream.
        view._librenms_api.get_device_info.assert_not_called()

    def test_no_flag_when_no_match(self):
        view = self._make_view()
        data = [{"ip_address": "10.0.0.1"}]
        view._flag_management_ip(data, "192.0.2.9")
        assert data[0].get("is_mgmt_ip") is None

    def test_no_flag_when_mgmt_ip_blank(self):
        view = self._make_view()
        data = [{"ip_address": "10.0.0.1"}]
        view._flag_management_ip(data, "")
        assert data[0].get("is_mgmt_ip") is None

    # ── _resolve_management_ip: the single live LibreNMS lookup (fetch path only) ──

    def test_resolve_returns_mgmt_ip(self):
        view = self._make_view()
        view._librenms_api.get_device_info.return_value = (True, {"ip": "10.0.0.1"})
        assert view._resolve_management_ip() == "10.0.0.1"
        # Must look up the management IP for *this* device, not some other ID.
        view._librenms_api.get_device_info.assert_called_once_with(view.librenms_id)

    def test_resolve_blank_when_no_librenms_id(self):
        view = self._make_view(librenms_id=None)
        assert view._resolve_management_ip() == ""
        view._librenms_api.get_device_info.assert_not_called()

    def test_resolve_blank_when_device_info_fails(self):
        view = self._make_view()
        view._librenms_api.get_device_info.return_value = (False, None)
        assert view._resolve_management_ip() == ""

    def test_resolve_blank_when_mgmt_ip_empty(self):
        view = self._make_view()
        view._librenms_api.get_device_info.return_value = (True, {"ip": ""})
        assert view._resolve_management_ip() == ""

    def test_resolve_blank_when_mgmt_ip_not_a_string(self):
        """A malformed-but-dict-shaped payload (e.g."""
        view = self._make_view()
        view._librenms_api.get_device_info.return_value = (True, {"ip": 123})
        assert view._resolve_management_ip() == ""

class TestSingleCableVerifyViewPermissionGate:
    """SingleCableVerifyView is a read-only JSON endpoint exposing a device's cable/topology rows; it must require dcim.view_device like the interface/module verify views."""

    def test_checks_permission_before_resolving_device(self):
        """The object-view gate must run BEFORE get_object_or_404 so an unauthorized caller can't probe arbitrary device IDs (existence via 404) or trigger LibreNMS work. Exercises the REAL require_object_permissions_json (only request.user.has_perm is mocked) — mocking the gate itself would mask a missing NetBoxObjectPermissionMixin base."""
        import json
        from unittest.mock import MagicMock, patch

        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        view = object.__new__(SingleCableVerifyView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        request = MagicMock()
        request.body = json.dumps({"device_id": 999, "local_port_id": "serial:1"}).encode()
        request.user.has_perm.return_value = False  # unauthorized → real gate returns 403
        view.request = request  # check_object_permissions reads self.request.user

        with patch("netbox_librenms_plugin.views.base.cables_view.get_object_or_404") as mock_get_obj:
            response = view.post(request)

        assert response.status_code == 403
        mock_get_obj.assert_not_called()  # device never resolved → no arbitrary-ID probing
