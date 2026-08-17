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

from dcim.models import Device

from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_ip
from netbox_librenms_plugin.tests.view_test_helpers import make_user_with_perms

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


class TestBaseCableTableViewGetPortsData:
    """get_ports_data must be safe to call before get_links_data sets self.librenms_id."""

    def test_get_ports_data_before_get_links_data_returns_empty(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock(server_key="default")
        # self.librenms_id is deliberately NOT set: get_links_data (its only in-tree caller)
        # hasn't run. The public method must degrade to the OOB-only/no-host result, not raise.
        result = view.get_ports_data(_mock_obj(), server_key="default")
        assert result == {"ports": []}


class TestBaseCableTableViewGetLinksData:
    """Tests for BaseCableTableView.get_links_data."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view.request = _mock_request()
        view.librenms_id = 42
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        # get_links_data resolves the id via get_librenms_id() and now coerces it (coerce_librenms_id
        # fails closed on a non-int/bool/zero value); a bare MagicMock return would coerce to None and
        # take the OOB-only "no host mapping" branch. Return a real id so the host-fetch path runs.
        view._librenms_api.get_librenms_id.return_value = 42
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

        def _fake_links(o, server_key=None, sync_device=None):
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

        def _fake_links(o, server_key=None, sync_device=None):
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
        """Non-dict rows in the ports payload (e.g. strings or None) are skipped, not .get()'d."""
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

    def test_oob_row_never_binds_a_host_interface(self):
        """A merged OOB LLDP row (context-only) must not resolve against the HOST device.

        Its local port lives on the OOB CONTROLLER; a shared name (or colliding stored
        librenms_id) would otherwise bind a host interface and render a wrong
        local_port_url + cable state — even though sync and actions already refuse OOB rows.
        """
        view = self._make_view()
        obj = make_device("cable-dev-oob-collide")
        make_interface(obj, "eth0")  # host interface sharing the OOB controller's port name

        link = {"local_port": "eth0", "_source": "oob"}
        view.enrich_local_port(link, obj)

        assert "local_port_url" not in link
        assert "netbox_local_interface_id" not in link

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
        """Issue #88: when the NetBox interface name matches the *non-selected* LibreNMS field (e.g. ifDescr while ifName is selected), the name fallback still resolves it."""
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

    def test_table_htmx_url_uses_resolved_server_scope(self):
        """The pagination/sort URL (table.htmx_url) is built from the RESOLVED server scope, not the lazy session client (which can point at another server after a failed rebind or global switch — silently swapping the dataset mid-view). It must be set in _prepare_context: DeviceCableTableView overrides get_table without calling super, so a base-class get_table override never ran for the device tab."""
        view = self._make_view()
        view._librenms_api.server_key = "default"  # session client points at ANOTHER server
        obj = _mock_obj()

        mock_table = MagicMock()
        with (
            patch.object(view, "get_cache_key", return_value="cable-key"),
            patch.object(view, "enrich_links_data", return_value=[]),
            patch.object(view, "get_table", return_value=mock_table),
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
        ):
            mock_cache.get.return_value = {"links": []}
            mock_cache.ttl.return_value = 300
            result = view._prepare_context(view.request, obj, fetch_fresh=False, server_key="secondary")

        assert result is not None
        assert isinstance(mock_table.htmx_url, str) and "server_key=secondary" in mock_table.htmx_url

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

    def test_unresolved_server_key_renders_empty_not_cached_rows(self):
        """?server_key naming a removed server must render EMPTY: its links snapshot can still be cached until TTL, and serving it as a live table (while the sibling tabs render empty) lets 'Create cable' act on a gone server's stale data."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()
        request.GET = {"server_key": "ghost"}

        stale_context = {"table": MagicMock(), "object": obj, "cache_expiry": None, "server_key": "ghost"}
        with (
            patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None),
            patch.object(view, "_prepare_context", return_value=stale_context) as prep,
        ):
            ctx = view.get_context_data(request, obj)

        prep.assert_not_called()  # the cached snapshot is never consulted
        assert ctx["table"] is None
        assert ctx["server_key"] == "ghost"


class TestBaseVLANTableViewUnresolvedServerKey:
    """BaseVLANTableView.get_vlan_context must honor the unresolved flag like its sibling tabs."""

    def test_unresolved_server_key_renders_empty_not_cached_vlans(self):
        """?server_key naming a removed server must render EMPTY, not the server's still-cached VLANs."""
        from netbox_librenms_plugin.views.base.vlan_table_view import BaseVLANTableView

        view = object.__new__(BaseVLANTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        obj = _mock_obj()
        request = _mock_request()
        request.GET = {"server_key": "ghost"}

        with (
            patch("netbox_librenms_plugin.librenms_api.build_librenms_api", return_value=None),
            patch("netbox_librenms_plugin.views.base.vlan_table_view.cache") as mock_cache,
            patch.object(view, "get_vlan_groups_for_device", return_value=[]),
        ):
            ctx = view.get_vlan_context(request, obj)

        mock_cache.get.assert_not_called()  # the removed server's snapshot is never consulted
        assert ctx["vlan_table"] is None
        assert ctx["server_key"] == "ghost"


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
            patch("netbox_librenms_plugin.views.mixins.render") as mock_render,
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
            patch("netbox_librenms_plugin.views.mixins.render") as mock_render,
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
        ):
            view.get_redirect_url = MagicMock(return_value="/back/")
            result = view.post(req, pk=1)

        # Rebind happens with the posted key, and the id lookup runs on the REBOUND client,
        # never the session one — proving the rebind swapped the client post() uses.
        mock_build.assert_called_once_with("prod")
        rebound_api.get_librenms_id.assert_called_once_with(obj)
        session_api.get_librenms_id.assert_not_called()
        # A real failure redirect came back (the rebound client has no host id).
        assert result.status_code == 302

    def test_post_stale_server_key_renders_migrated_context(self):
        """A posted server_key that no longer resolves (build returns None) → error + partial render with migrated context under the session key (NOT a redirect, which an HTMX swap would mishandle and which would drop migrated-donor suppression)."""
        from unittest.mock import patch

        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        view = object.__new__(BaseInterfaceTableView)
        view.partial_template_name = "test_template.html"
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
            patch(
                "netbox_librenms_plugin.utils.build_migrated_context",
                return_value={"migrated_to_marker": {"device_id": 7}},
            ) as mock_migrated,
            patch("netbox_librenms_plugin.views.mixins.render", return_value="rendered") as mock_render,
        ):
            result = view.post(req, pk=1)

        # Never reached the live id lookup; surfaced an error and rendered the partial.
        session_api.get_librenms_id.assert_not_called()
        mock_messages.error.assert_called_once()
        assert result == "rendered"
        # Migrated context resolved under the session/active key — NOT the stale POSTed "ghost".
        mock_migrated.assert_called_once_with(obj, "default")
        ctx = mock_render.call_args.args[2]
        assert ctx["migrated_to_marker"] == {"device_id": 7}
        # The stale-key render must also disable live sync state: interface_sync.server_key is None.
        assert ctx["interface_sync"]["server_key"] is None

    def test_ip_post_stale_server_key_keeps_migrated_context(self):
        """IP sync's stale-server branch must include build_migrated_context so a migrated donor keeps its suppressed sync form/button — a stale server_key must not silently re-enable IP sync."""
        from unittest.mock import patch

        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        view = object.__new__(BaseIPAddressTableView)
        view._librenms_api = MagicMock(server_key="session-key")
        obj = MagicMock(pk=1)
        view.get_object = MagicMock(return_value=obj)
        view.rebind_api_for_server = MagicMock(return_value=None)  # stale key → rebind fails

        req = MagicMock()
        req.POST.get.side_effect = lambda k, d=None: {"server_key": "ghost"}.get(k, d)

        with (
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.get_interface_name_field", return_value="name"),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.messages") as mock_messages,
            patch(
                "netbox_librenms_plugin.utils.build_migrated_context",
                return_value={"migrated_to_marker": {"device_id": 7}},
            ) as mock_migrated,
            patch("netbox_librenms_plugin.views.mixins.render", return_value="rendered") as mock_render,
        ):
            result = view.post(req, pk=1)

        mock_messages.error.assert_called_once()
        # Migrated context resolved under the session/active key — NOT the stale POSTed "ghost"
        # (which failed to rebind and would miss the marker, re-enabling the donor's sync controls).
        mock_migrated.assert_called_once_with(obj, "session-key")
        ctx = mock_render.call_args.args[2]
        assert ctx["migrated_to_marker"] == {"device_id": 7}
        # The stale-key render must also disable live IP sync state: ip_sync.server_key is None
        # so the template can't expose the stale key back to the live-sync controls. Without this
        # the test would pass even if a stale key leaked into the live-sync context.
        assert ctx["ip_sync"]["server_key"] is None
        assert result == "rendered"

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


@pytest.mark.django_db
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

    def test_post_failed_rebind_renders_partial_not_redirect(self):
        """A stale POSTed server_key renders the fragment with the error, NOT a redirect: the redirect target is this same POST-only URL, so the hx-post XHR would follow it with GET → 405, no swap, a dead button (every sibling tab renders its partial here)."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        with (
            patch.object(view, "get_object", return_value=obj),
            patch.object(view, "get_redirect_url", return_value="/device/1/"),
            patch.object(view, "rebind_api_for_server", return_value=None),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages") as mock_messages,
            # post() renders here via interfaces_view.render; a higher branch refactors that
            # to render_sync_partial() (mixins.render). Patch both (create=True) so this test
            # stays valid as it rides up the stack regardless of which render site post() uses.
            patch(
                "netbox_librenms_plugin.views.base.interfaces_view.render",
                return_value=MagicMock(status_code=200),
                create=True,
            ) as mock_render_local,
            patch(
                "netbox_librenms_plugin.views.mixins.render",
                return_value=MagicMock(status_code=200),
                create=True,
            ) as mock_render_mixin,
        ):
            response = view.post(request, pk=1)

        mock_messages.error.assert_called_once()
        # The fragment is rendered in place with the message — no 302 anywhere.
        calls = mock_render_local.call_args_list + mock_render_mixin.call_args_list
        assert len(calls) == 1
        args = calls[0][0]
        assert args[1] == view.partial_template_name
        ctx = args[2]
        assert ctx["interface_sync"]["table"] is None
        # Explicit None: the fragment must not fall back to the session/default server.
        assert ctx["interface_sync"]["server_key"] is None
        expected = mock_render_local.return_value if mock_render_local.called else mock_render_mixin.return_value
        assert response is expected  # the rendered fragment, not a 302

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
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
        ):
            response = view.post(request, pk=1)

        mock_messages.error.assert_called_once()
        # The failure redirect must preserve the POST-scoped server_key so the user stays on
        # the same LibreNMS server for the next retry (real HttpResponseRedirect via the shared
        # redirect_with_server_key helper).
        assert response.url == "/device/1/?server_key=prod"
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
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
        ):
            response = view.post(request, pk=1)

        mock_messages.error.assert_called_once_with(request, "Connection refused")
        # Failure redirect preserves the POST-scoped server_key (see redirect_with_server_key).
        assert response.url == "/device/1/?server_key=prod"
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
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
        ):
            response = view.post(request, pk=1)

        mock_messages.error.assert_called_once_with(
            request, "Unexpected response from LibreNMS (malformed ports payload)."
        )
        assert response.url == "/device/1/?server_key=prod"
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
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
        ):
            response = view.post(request, pk=1)

        mock_messages.error.assert_called_once_with(
            request, "Unexpected response from LibreNMS (malformed ports payload)."
        )
        assert response.url == "/device/1/?server_key=prod"
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
            # The open-redirect barrier now lives in the shared mixins.redirect_with_server_key.
            patch(
                "netbox_librenms_plugin.views.mixins.url_has_allowed_host_and_scheme",
                return_value=False,
            ) as mock_barrier,
        ):
            response = view.post(request, pk=1)

        # post() must read the POSTed server_key (proving the redirect candidate really is
        # POST-derived), then the barrier must be consulted and the tainted key dropped on reject.
        mock_rebind.assert_called_once_with("prod")
        mock_barrier.assert_called_once()
        # Pin the barrier INPUT, not just that it was called: the candidate URL gated through
        # url_has_allowed_host_and_scheme must be the one carrying the POST-derived server_key.
        # Otherwise a regression that validated the bare "/device/1/" and appended the key after
        # the check would still pass this test while reintroducing the open-redirect path.
        assert mock_barrier.call_args.args[0] == "/device/1/?server_key=prod"
        # Barrier rejected → the tainted key is dropped, redirecting to the bare URL.
        assert response.url == "/device/1/"

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
            patch("netbox_librenms_plugin.views.mixins.render") as mock_render,
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
            patch("netbox_librenms_plugin.views.mixins.render") as mock_render,
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
            patch.object(view, "get_context_data", return_value={}),
            patch.object(view, "get_cache_key", return_value="cache-key"),
            patch.object(view, "get_last_fetched_key", return_value="last-key"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_oob", return_value={"id": 99}),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.mixins.render") as mock_render,
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
            patch.object(view, "get_context_data", return_value={}),
            patch.object(view, "get_cache_key", return_value="cache-key"),
            patch.object(view, "get_last_fetched_key", return_value="last-key"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_oob", return_value={"id": 99}),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages"),
            patch("netbox_librenms_plugin.views.mixins.render") as mock_render,
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
            patch.object(view, "get_context_data", return_value={}),
            patch.object(view, "get_cache_key", return_value="cache-key"),
            patch.object(view, "get_last_fetched_key", return_value="last-key"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_oob", return_value={"id": 99}),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages"),
            patch("netbox_librenms_plugin.views.mixins.render") as mock_render,
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
            patch.object(view, "get_context_data", return_value={}),
            patch.object(view, "get_cache_key", return_value="cache-key"),
            patch.object(view, "get_last_fetched_key", return_value="last-key"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_oob", return_value={"id": 99}),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.mixins.render") as mock_render,
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

    def test_post_oob_invalid_id_marks_snapshot_incomplete(self):
        """A corrupt stored OOB id fails closed: warn, skip the OOB fetch, tag the snapshot oob_incomplete."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        view._librenms_api.get_librenms_id.return_value = 42
        # Host ports OK. The OOB controller IS linked but its stored id is non-numeric, so the OOB
        # fetch must be skipped entirely — get_ports is called exactly once (host), never for OOB.
        view._librenms_api.get_ports.return_value = (True, {"ports": [{"port_id": 1, "ifName": "Gi0/0"}]})

        with (
            patch.object(view, "get_object", return_value=obj),
            patch.object(view, "get_redirect_url", return_value="/device/1/"),
            patch.object(view, "_enrich_ports_with_vlan_data", side_effect=lambda ports, field: ports),
            patch.object(view, "get_context_data", return_value={}),
            patch.object(view, "get_cache_key", return_value="cache-key"),
            patch.object(view, "get_last_fetched_key", return_value="last-key"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_interface_name_field", return_value="ifName"),
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_sync_device", return_value=obj),
            patch(
                "netbox_librenms_plugin.views.base.interfaces_view.get_librenms_oob",
                return_value={"id": "not-a-number"},
            ),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.mixins.render") as mock_render,
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.interfaces_view.timezone"),
        ):
            mock_render.return_value = MagicMock()
            view.post(request, pk=1)

        # The corrupt id never reaches a device-scoped LibreNMS call: only the host fetch runs.
        view._librenms_api.get_ports.assert_called_once_with(42)

        # The user is warned that OOB ports are missing (not silently dropped).
        warning_msgs = [a for call in mock_messages.warning.call_args_list for a in call.args if isinstance(a, str)]
        assert any("OOB controller ports fetch failed" in m for m in warning_msgs)

        # The cached snapshot is tagged incomplete so get_context_data keeps the banner on later
        # cached renders — without the fix oob_ports_failed stayed False and this flag was absent.
        ports_set_calls = [c for c in mock_cache.set.call_args_list if c.args and c.args[0] == "cache-key"]
        assert len(ports_set_calls) == 1
        assert ports_set_calls[0].args[1].get("oob_incomplete") is True

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
            patch("netbox_librenms_plugin.views.base.interfaces_view.get_librenms_oob", return_value=None),
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages") as mock_messages,
            patch("netbox_librenms_plugin.views.mixins.render") as mock_render,
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

    def test_post_lag_inference_excludes_oob_ports(self):
        """port_stack is scoped to the main device, so its lazy-fetch trigger must ignore OOB rows."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        view._librenms_api.get_librenms_id.return_value = 42
        # Host ports carry no LAG signal; the OOB controller exposes a LAG-typed port.
        view._librenms_api.get_ports.side_effect = [
            (True, {"ports": [{"port_id": 1, "ifName": "Gi0/0", "ifType": "ethernetCsmacd"}]}),
            (True, {"ports": [{"port_id": 2, "ifName": "Po1", "ifType": "ieee8023adLag"}]}),
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
            patch("netbox_librenms_plugin.views.base.interfaces_view.messages"),
            patch("netbox_librenms_plugin.views.mixins.render") as mock_render,
            patch("netbox_librenms_plugin.views.base.interfaces_view.cache") as mock_cache,
            patch("netbox_librenms_plugin.views.base.interfaces_view.timezone"),
        ):
            mock_render.return_value = MagicMock()
            view.post(request, pk=1)

        # Prove the OOB path actually ran end-to-end first — otherwise the assertions below
        # would also pass if OOB ports were never fetched or merged (the whole point of the
        # filter being that there *was* an OOB LAG row to exclude).
        view._librenms_api.get_ports.assert_any_call(99)  # OOB controller ports fetched
        cached_snapshot = next(
            call.args[1] for call in mock_cache.set.call_args_list if call.args and call.args[0] == "cache-key"
        )
        assert any(p.get("_source") == "oob" for p in cached_snapshot["ports"]), (
            "OOB row was never merged into the snapshot — the test would pass vacuously"
        )

        # The OOB LAG row does not trigger the main-device port_stack fetch.
        view._librenms_api.get_port_stack.assert_not_called()


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
            # get_object now scopes by request.user, so the request needs a real permitted user.
            request.user = make_user_with_perms("coerce-iface-viewer", [("view", Device)])
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


def _real_api_for_coerce():
    """A real LibreNMSAPI bound to a stub 'default' server (no real HTTP made by these tests)."""
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


@pytest.mark.django_db
class TestBaseVLANTablePostCoercesLibreNMSId:
    """VLAN refresh must coerce the (cache-path) librenms_id before get_device_vlans()."""

    def test_corrupt_cached_id_fails_closed_before_get_device_vlans(self):
        """A boolean cached under the device-id key fails closed at the view, never reaching get_device_vlans()."""
        from django.core.cache import cache as real_cache
        from django.test import RequestFactory

        from netbox_librenms_plugin.views.object_sync.devices import DeviceVLANTableView

        device = make_device("coerce-vlan-host")  # no librenms_id CF → forces the cache path
        api = _real_api_for_coerce()
        real_cache.set(api._get_cache_key(device), True)
        try:
            assert api.get_librenms_id(device) is True  # the uncoerced cache path really returns the bool

            view = object.__new__(DeviceVLANTableView)
            view._librenms_api = api
            view.rebind_api_for_server = MagicMock(return_value="default")  # keep the real api bound
            api.get_device_vlans = MagicMock(name="get_device_vlans", return_value=(False, "should-not-be-called"))

            request = RequestFactory().post(f"/plugins/librenms/devices/{device.pk}/vlan-sync/", data={})
            # get_object now scopes by request.user, so the request needs a real permitted user.
            request.user = make_user_with_perms("coerce-vlan-viewer", [("view", Device)])
            view.request = request
            # The error-path render moves up the stack: this branch returns render(...), but a
            # higher branch routes the same exit through self.render_sync_partial(). Patch BOTH with
            # create=True so the test passes on every branch regardless of which one is the live exit.
            view.render_sync_partial = MagicMock(return_value=MagicMock())
            with (
                patch("netbox_librenms_plugin.views.base.vlan_table_view.messages") as mock_messages,
                patch(
                    "netbox_librenms_plugin.views.base.vlan_table_view.render",
                    return_value=MagicMock(),
                    create=True,
                ),
            ):
                view.post(request, pk=device.pk)
        finally:
            real_cache.delete(api._get_cache_key(device))

        assert view.librenms_id is None
        api.get_device_vlans.assert_not_called()
        mock_messages.error.assert_called_once_with(request, "Device not found in LibreNMS.")


@pytest.mark.django_db
class TestBaseModuleTablePostCoercesLibreNMSId:
    """Module refresh must coerce the (cache-path) librenms_id before get_device_inventory()."""

    def test_corrupt_cached_id_fails_closed_before_get_device_inventory(self):
        """A boolean cached under the device-id key fails closed at the view, never reaching get_device_inventory()."""
        from django.core.cache import cache as real_cache
        from django.test import RequestFactory

        from netbox_librenms_plugin.views.object_sync.devices import DeviceModuleTableView

        device = make_device("coerce-mod-host")  # no librenms_id CF → forces the cache path
        api = _real_api_for_coerce()
        real_cache.set(api._get_cache_key(device), True)
        try:
            assert api.get_librenms_id(device) is True

            view = object.__new__(DeviceModuleTableView)
            view._librenms_api = api
            view.rebind_api_for_server = MagicMock(return_value="default")
            view.has_write_permission = MagicMock(return_value=True)
            api.get_device_inventory = MagicMock(
                name="get_device_inventory", return_value=(False, "should-not-be-called")
            )

            request = RequestFactory().post(f"/plugins/librenms/devices/{device.pk}/module-sync/", data={})
            # get_object now scopes by request.user, so the request needs a real permitted user.
            request.user = make_user_with_perms("coerce-module-viewer", [("view", Device)])
            view.request = request
            # The error-path render moves up the stack (return render(...) here vs
            # self.render_sync_partial() on a higher branch); patch BOTH with create=True so the
            # test is branch-agnostic.
            view.render_sync_partial = MagicMock(return_value=MagicMock())
            with (
                patch("netbox_librenms_plugin.views.base.modules_view.messages") as mock_messages,
                patch(
                    "netbox_librenms_plugin.views.base.modules_view.render",
                    return_value=MagicMock(),
                    create=True,
                ),
            ):
                view.post(request, pk=device.pk)
        finally:
            real_cache.delete(api._get_cache_key(device))

        assert view.librenms_id is None
        api.get_device_inventory.assert_not_called()
        mock_messages.error.assert_called_once_with(request, "Device not found in LibreNMS.")


class TestBaseInterfaceTableViewGetContextData:
    """Tests for BaseInterfaceTableView.get_context_data."""

    @staticmethod
    def _make_real_device_view(device):
        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.view_test_helpers import make_request
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        request = make_request("get", path=f"/plugins/librenms/device/{device.pk}/interfaces/")
        view = DeviceInterfaceTableView()
        view.setup(request)
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        view._librenms_api = api
        return view, request

    def _cached_device_context(self, device, snapshot):
        from django.core.cache import cache

        view, request = self._make_real_device_view(device)
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(cache_key, snapshot)
        try:
            return view.get_context_data(request, device, "ifName", "default")
        finally:
            cache.delete(cache_key)

    def test_vm_interface_lookup_selects_parent(self, db, django_assert_num_queries):
        """VM relationship rendering must not fetch each parent in a separate query."""
        from virtualization.models import VMInterface

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.conftest import make_vm
        from netbox_librenms_plugin.tests.view_test_helpers import make_request
        from netbox_librenms_plugin.views.object_sync.vms import VMInterfaceTableView

        vm = make_vm("interface-context-parent-prefetch")
        parent = VMInterface.objects.create(virtual_machine=vm, name="eth0")
        VMInterface.objects.create(virtual_machine=vm, name="eth0.100", parent=parent)
        view = VMInterfaceTableView()
        view.setup(make_request("get"))
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        view._librenms_api = api

        maps = view._build_interface_lookup_maps(vm)

        with django_assert_num_queries(0):
            assert maps["by_name"]["eth0.100"].parent.pk == parent.pk

    def test_cache_miss_returns_empty_table(self, db):
        """When no cached data, table is None."""
        from django.core.cache import cache

        device = make_device("interface-context-cache-miss")
        view, request = self._make_real_device_view(device)
        cache_key = view.get_cache_key(device, "ports", "default")
        last_fetched_key = view.get_last_fetched_key(device, "ports", "default")
        overrides_key = view.get_vlan_overrides_key(device, "default")
        cache.delete_many((cache_key, last_fetched_key, overrides_key))

        ctx = view.get_context_data(request, device, "ifName", "default")

        assert ctx["table"] is None

    def test_malformed_non_dict_cache_degrades_to_empty_table(self, db):
        """A truthy but non-dict ports cache entry (legacy/older-shape or corrupt) must degrade to an empty table, not AttributeError-500 on .get('ports')."""
        from django.core.cache import cache

        device = make_device("interface-context-malformed-cache")
        view, request = self._make_real_device_view(device)
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(cache_key, [{"ports": []}])
        try:
            ctx = view.get_context_data(request, device, "ifName", "default")
        finally:
            cache.delete(cache_key)

        assert ctx["table"] is None

    def test_cache_hit_non_vc_builds_table(self, db):
        """Cached data without VC produces table."""
        from django.core.cache import cache

        device = make_device("interface-context-cache-hit")
        make_interface(device, "Gi0/0")
        view, request = self._make_real_device_view(device)
        snapshot = {
            "ports": [{"port_id": 1, "ifName": "Gi0/0", "ifAdminStatus": "up", "ifAlias": None, "ifDescr": "Gi0/0"}]
        }
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.set(cache_key, snapshot)
        try:
            ctx = view.get_context_data(request, device, "ifName", "default")
        finally:
            cache.delete(cache_key)

        assert ctx["table"] is not None
        assert len(ctx["table"].data) == 1
        # A complete snapshot is not flagged incomplete.
        assert ctx["oob_incomplete"] is False

    def test_hidden_interface_state_is_not_rendered_outside_view_grant(self, db):
        from dcim.models import Device, Interface

        from netbox_librenms_plugin.tests.view_test_helpers import grant, make_request
        from netbox_librenms_plugin.utils import set_librenms_device_id

        device = make_device("interface-context-hidden")
        hidden = make_interface(device, "Ethernet1")
        set_librenms_device_id(hidden, 101, "default")
        hidden.save()
        decoy = make_interface(make_device("interface-context-visible"), "Ethernet2")
        user = make_user_with_perms(
            "interface-context-scoped-viewer",
            [("view", Device)],
            constraints={"pk": device.pk},
        )
        user = grant(user, "view", Interface, constraints={"pk": decoy.pk})
        request = make_request("get", user=user, path=f"/plugins/librenms/device/{device.pk}/interfaces/")

        view, _default_request = self._make_real_device_view(device)
        view.setup(request)
        context = view.get_context_data(
            request,
            device,
            "ifName",
            "default",
            fresh_data={
                "ports": [
                    {
                        "port_id": 101,
                        "ifName": hidden.name,
                        "ifDescr": hidden.name,
                        "ifAlias": "",
                        "ifType": "ethernetCsmacd",
                        "ifSpeed": 1_000_000_000,
                        "ifPhysAddress": "",
                        "ifMtu": 1500,
                        "ifAdminStatus": "up",
                    }
                ]
            },
            sync_device=device,
        )
        response = view.render_sync_partial(
            request,
            device,
            "default",
            {"interface_sync": context},
        )
        html = response.content.decode()

        assert context["table"].data[0]["netbox_interface"] is None
        assert '<span class="text-success">Enabled</span>' not in html
        assert '<span class="text-danger">Enabled</span>' in html

    def test_get_context_data_survives_cache_backend_without_ttl(self, db):
        """cache.ttl() is Redis-specific and not part of the Django cache API."""
        device = make_device("interface-context-cache-without-ttl")
        view, request = self._make_real_device_view(device)
        snapshot = {"ports": [{"port_id": 1, "ifName": "Gi0/0", "ifAdminStatus": "up", "ifDescr": "Gi0/0"}]}
        cache_key = view.get_cache_key(device, "ports", "default")

        class CacheWithoutTTL:
            def get(self, key, default=None):
                return snapshot if key == cache_key else default

            def delete(self, _key):
                return True

        with patch("netbox_librenms_plugin.views.base.interfaces_view.cache", CacheWithoutTTL()):
            ctx = view.get_context_data(request, device, "ifName", "default")

        assert ctx["table"] is not None
        assert ctx["cache_expiry"] is None

    def test_oob_incomplete_flag_surfaced_from_cache(self, db):
        """A cached snapshot tagged oob_incomplete surfaces the flag in context so the template can warn that OOB rows are missing."""
        device = make_device("interface-context-oob-incomplete")
        snapshot = {
            "ports": [{"port_id": 1, "ifName": "Gi0/0", "ifAdminStatus": "up", "ifAlias": None, "ifDescr": "Gi0/0"}],
            "oob_incomplete": True,
        }
        ctx = self._cached_device_context(device, snapshot)

        assert ctx["oob_incomplete"] is True

    def test_relationship_data_incomplete_flag_surfaced_from_cache(self, db):
        """A failed relationship fetch remains visible on every cached render."""
        device = make_device("interface-context-relationships-incomplete")
        snapshot = {
            "ports": [{"port_id": 1, "ifName": "Gi0/0", "ifAdminStatus": "up", "ifAlias": None, "ifDescr": "Gi0/0"}],
            "relationship_data_incomplete": True,
        }
        ctx = self._cached_device_context(device, snapshot)

        assert ctx["relationship_data_incomplete"] is True

    def test_relationship_data_incomplete_defaults_false(self, db):
        """A snapshot without the flag does not show a spurious relationship warning."""
        device = make_device("interface-context-relationships-complete")
        snapshot = {
            "ports": [{"port_id": 1, "ifName": "Gi0/0", "ifAdminStatus": "up", "ifAlias": None, "ifDescr": "Gi0/0"}],
        }
        ctx = self._cached_device_context(device, snapshot)

        assert ctx["relationship_data_incomplete"] is False

    def test_cache_hit_with_vc_uses_vc_members(self, db):
        """Cached VC rows resolve against real chassis members."""
        from netbox_librenms_plugin.tests.conftest import make_virtual_chassis_members
        from netbox_librenms_plugin.utils import set_librenms_device_id

        _virtual_chassis, (member1, member2) = make_virtual_chassis_members("interface-context-vc")
        interface = make_interface(member2, "Ethernet2")
        set_librenms_device_id(interface, 1, "default")
        interface.save()
        snapshot = {
            "ports": [
                {
                    "port_id": 1,
                    "ifName": "Ethernet2",
                    "ifType": "ethernetCsmacd",
                    "ifAdminStatus": "up",
                }
            ]
        }

        ctx = self._cached_device_context(member1, snapshot)

        assert {member.pk for member in ctx["virtual_chassis_members"]} == {member1.pk, member2.pk}
        assert next(iter(ctx["table"].data))["selected_object_id"] == member2.pk

    def test_cache_hit_non_vc_ignores_duplicate_librenms_ids(self, db):
        """Conflicting interface librenms_id values must not create an arbitrary port-id match."""
        from netbox_librenms_plugin.utils import set_librenms_device_id

        device = make_device("interface-context-duplicate-id")
        interface_a = make_interface(device, "Gi0/0")
        interface_b = make_interface(device, "Gi0/1")
        for interface in (interface_a, interface_b):
            set_librenms_device_id(interface, 101, "default")
            interface.save()
        snapshot = {
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
        ctx = self._cached_device_context(device, snapshot)
        row = next(iter(ctx["table"].data))

        assert row["exists_in_netbox"] is False
        assert row["netbox_interface"] is None


@pytest.mark.django_db
class TestVlanGroupOverrideScope:
    """Overrides must be validated against the row's in-scope groups, with real ORM objects."""

    def _view(self):
        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        return object.__new__(BaseInterfaceTableView)

    def _fixtures(self, slug):
        from dcim.models import Site
        from django.contrib.contenttypes.models import ContentType
        from ipam.models import VLANGroup

        site = Site.objects.create(name=f"Override {slug}", slug=f"override-{slug}", status="active")
        site_type = ContentType.objects.get_for_model(Site)
        in_scope = VLANGroup.objects.create(
            name=f"In scope {slug}", slug=f"in-scope-{slug}", scope_type=site_type, scope_id=site.pk
        )
        other_site = Site.objects.create(name=f"Other {slug}", slug=f"other-{slug}", status="active")
        out_of_scope = VLANGroup.objects.create(
            name=f"Out of scope {slug}", slug=f"out-of-scope-{slug}", scope_type=site_type, scope_id=other_site.pk
        )
        return site, in_scope, out_of_scope

    def test_override_to_an_in_scope_group_missing_the_vid_is_kept(self):
        """The group need not already contain the VLAN: that is what "apply to all" is for."""
        from dcim.models import Device

        _site, in_scope, _out = self._fixtures("keep")
        port = {"untagged_vlan": 100, "tagged_vlans": [], "vlan_groups": [in_scope]}
        # vid_to_groups is empty: no existing group carries VID 100 yet.
        lookup_maps = {"vid_to_groups": {}, "vid_group_to_vlan": {}}

        self._view()._add_vlan_group_selection(port, lookup_maps, Device(), {"100": str(in_scope.pk)})

        assert port["vlan_group_map"][100]["group_id"] == str(in_scope.pk)
        assert port["vlan_group_map"][100]["group_name"] == in_scope.name

    def test_override_to_a_group_outside_the_row_scope_is_rejected(self):
        """Negative control: an out-of-scope group must not be honoured."""
        from dcim.models import Device

        _site, in_scope, out_of_scope = self._fixtures("reject")
        port = {"untagged_vlan": 100, "tagged_vlans": [], "vlan_groups": [in_scope]}
        lookup_maps = {"vid_to_groups": {}, "vid_group_to_vlan": {}}

        self._view()._add_vlan_group_selection(port, lookup_maps, Device(), {"100": str(out_of_scope.pk)})

        assert port["vlan_group_map"][100]["group_id"] == ""
        assert port["vlan_group_map"][100]["group_name"] == "Global"


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
        default_group = VLANGroup.objects.create(name="Default-Group", slug="default-group")
        override_group = VLANGroup.objects.create(name="Override-Group", slug="override-group")
        # Production always sets vlan_groups on the row before calling; without it the helper
        # would be validating overrides against a scope the caller never supplied.
        port = {"untagged_vlan": 100, "tagged_vlans": [], "vlan_groups": [default_group, override_group]}
        lookup_maps = {"vid_to_groups": {100: [default_group, override_group]}}
        device = make_device("vlan-ovr-dev")

        view._add_vlan_group_selection(port, lookup_maps, device, vlan_group_overrides={"100": str(override_group.pk)})

        assert port["vlan_group_map"][100]["group_id"] == str(override_group.pk)
        assert port["vlan_group_map"][100]["group_name"] == "Override-Group"

    @pytest.mark.django_db
    def test_malformed_vlan_group_override_keeps_the_automatic_selection(self):
        """A malformed cached override must not abort interface table enrichment."""
        from ipam.models import VLANGroup

        view = self._make_view()
        available_group = VLANGroup.objects.create(name="Available Group", slug="available-group")
        port = {"untagged_vlan": 100, "tagged_vlans": []}
        lookup_maps = {"vid_to_groups": {100: [available_group]}}
        device = make_device("malformed-vlan-override")

        view._add_vlan_group_selection(
            port,
            lookup_maps,
            device,
            vlan_group_overrides={"100": "not-a-group-id"},
        )

        assert port["vlan_group_map"][100]["group_id"] == str(available_group.pk)
        assert port["vlan_group_map"][100]["group_name"] == "Available Group"

    @pytest.mark.django_db
    def test_boolean_vlan_group_override_keeps_the_automatic_selection(self):
        """A boolean cached override must not select the Global VLAN scope."""
        from ipam.models import VLANGroup

        view = self._make_view()
        available_group = VLANGroup.objects.create(name="Boolean Override Group", slug="boolean-override-group")
        port = {"untagged_vlan": 100, "tagged_vlans": []}
        lookup_maps = {
            "vid_to_groups": {100: [available_group]},
            "vid_group_to_vlan": {(100, None): object()},
        }
        device = make_device("boolean-vlan-override")

        view._add_vlan_group_selection(
            port,
            lookup_maps,
            device,
            vlan_group_overrides={"100": False},
        )

        assert port["vlan_group_map"][100]["group_id"] == str(available_group.pk)
        assert port["vlan_group_map"][100]["group_name"] == "Boolean Override Group"

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

    def test_no_valid_format_raises_value_error(self):
        """When no supported IP fields exist, the boundary rejects the row."""
        view = self._make_view()
        ip_entry = {"port_id": 1}  # No IP address fields
        obj = MagicMock()

        with pytest.raises(ValueError, match="no supported address fields"):
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
            "ip_addresses_map": {"192.168.1.1/24": [existing_ip]},
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

        # A realistic cached row: the cached branch now validates each row (port_id + an
        # address/prefix pair) before reuse, so an incomplete stub would be purged as malformed.
        cached_ips = [
            {
                "port_id": 7,
                "ip_address": "192.168.1.1",
                "prefix_length": 24,
                "ip_with_mask": "192.168.1.1/24",
                "status": "matched",
            }
        ]
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
            patch("netbox_librenms_plugin.views.mixins.render") as mock_render,
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
            patch("netbox_librenms_plugin.views.mixins.render") as mock_render,
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
        unreported = make_ip("198.51.100.99/24")

        result = view._prefetch_netbox_data(obj, {"198.51.100.5/24"})

        # Only the reported address is scanned; an unrelated IPAM row stays out of the map.
        assert str(unreported.address) not in result["ip_addresses_map"]
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

        result = view._prefetch_netbox_data(obj, set())

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

    @pytest.mark.django_db
    def test_get_object_resolves_through_a_restricted_queryset(self):
        """get_object returns a permitted object and 404s on one outside the caller's grant."""
        from dcim.models import Device
        from django.http import Http404

        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.tests.view_test_helpers import (
            make_request,
            make_user_with_perms,
            make_view,
        )
        from netbox_librenms_plugin.views.base.interfaces_view import BaseInterfaceTableView

        allowed = make_device("getobj-allowed-iface")
        hidden = make_device("getobj-hidden-iface")
        user = make_user_with_perms("getobj-viewer-iface", [("view", Device)], constraints={"name": allowed.name})
        view = make_view(BaseInterfaceTableView, make_request("get", user=user))
        view.model = Device

        assert view.get_object(allowed.pk).pk == allowed.pk
        with pytest.raises(Http404):
            view.get_object(hidden.pk)

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

    def test_get_context_data_with_none_interface_name_field_uses_request_preference(self, db):
        """A missing explicit field uses the real request preference."""
        from django.core.cache import cache

        from netbox_librenms_plugin.librenms_api import LibreNMSAPI
        from netbox_librenms_plugin.tests.view_test_helpers import make_request
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        device = make_device("interface-context-name-preference")
        request = make_request("get", {"interface_name_field": "ifDescr"})
        view = DeviceInterfaceTableView()
        view.setup(request)
        api = object.__new__(LibreNMSAPI)
        api.server_key = "default"
        view._librenms_api = api
        cache_key = view.get_cache_key(device, "ports", "default")
        cache.delete(cache_key)

        ctx = view.get_context_data(request, device, interface_name_field=None, server_key="default")

        assert ctx["interface_name_field"] == "ifDescr"

    # ---- Real-DB coverage for get_context_data (concrete DeviceInterfaceTableView) ----
    # These replace the prior MagicMock-only versions, which fed a bare MagicMock request into the
    # real server-key resolution: request.GET.get("server_key") returned truthy garbage, so
    # rebind_api_for_server -> build_librenms_api(garbage) resolved against ambient config/DB state
    # and flaked under full-suite ordering (nulling cached_data -> the lines under test never ran).
    # Here everything is real (Device/VC/Interface + Django cache); only build_librenms_api, the
    # LibreNMS HTTP-client factory boundary, is faked so no server is contacted.

    @staticmethod
    def _real_view_and_request():
        from django.contrib.auth import get_user_model
        from django.test import RequestFactory

        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        view = DeviceInterfaceTableView()
        request = RequestFactory().get("/")  # GET render, no ?server_key -> default server
        user_model = get_user_model()
        request.user = user_model.objects.first() or user_model.objects.create_user(username="iface-ctx-tester")
        view.request = request
        return view, request

    @staticmethod
    def _seed_ports(view, device, ports):
        from django.core.cache import cache as real_cache

        from netbox_librenms_plugin.utils import get_librenms_sync_device

        # Scope the seed to the exact key the view will read (VC devices scope to the sync device).
        cache_device = get_librenms_sync_device(device, server_key="default") or device
        real_cache.set(view.get_cache_key(cache_device, "ports", "default"), {"ports": ports})

    @pytest.mark.django_db
    def test_ifalias_cleared_when_matches_ifdescr(self):
        """A cached port whose ifAlias equals its ifDescr renders with ifAlias cleared to ''."""
        from unittest.mock import MagicMock, patch

        device = make_device("iface-ctx-ifalias")
        make_interface(device, "Gi0/0", iface_type="1000base-t")
        view, request = self._real_view_and_request()
        self._seed_ports(
            view,
            device,
            [
                {
                    "port_id": 1,
                    "ifName": "Gi0/0",
                    "ifDescr": "GigabitEthernet0/0",
                    "ifAlias": "GigabitEthernet0/0",  # equals ifDescr -> cleared
                    "ifAdminStatus": "up",
                }
            ],
        )

        with patch(
            "netbox_librenms_plugin.librenms_api.build_librenms_api",
            return_value=MagicMock(server_key="default"),
        ):
            ctx = view.get_context_data(request, device, "ifName")

        rendered = next(p for p in ctx["table"].data if p.get("ifName") == "Gi0/0")
        assert rendered["ifAlias"] == ""

    @pytest.mark.django_db
    def test_netbox_only_interface_vc_gets_member_device_name(self):
        """A VC member interface absent from LibreNMS is flagged netbox-only with the member's name."""
        from unittest.mock import MagicMock, patch

        from dcim.models import VirtualChassis

        vc = VirtualChassis.objects.create(name="vc-nbonly")
        switch = make_device("vc-nbonly-switch")
        switch.virtual_chassis = vc
        switch.vc_position = 1
        switch.save()
        make_interface(switch, "Gi0/1", iface_type="1000base-t")  # only in NetBox

        view, request = self._real_view_and_request()
        self._seed_ports(view, switch, [{"port_id": 1, "ifName": "Gi0/0", "ifAdminStatus": "up", "ifAlias": "x"}])

        with patch(
            "netbox_librenms_plugin.librenms_api.build_librenms_api",
            return_value=MagicMock(server_key="default"),
        ):
            ctx = view.get_context_data(request, switch, "ifName")

        gi01 = next((i for i in ctx["netbox_only_interfaces"] if i["name"] == "Gi0/1"), None)
        assert gi01 is not None
        assert gi01["device_name"] == "vc-nbonly-switch"

    @pytest.mark.django_db
    def test_netbox_only_interface_non_vc_gets_obj_device_name(self):
        """A non-VC device interface absent from LibreNMS is flagged netbox-only with the device name."""
        from unittest.mock import MagicMock, patch

        device = make_device("router-nbonly")
        make_interface(device, "Gi0/0", iface_type="1000base-t")  # matched by the cached port
        make_interface(device, "Gi0/1", iface_type="1000base-t")  # only in NetBox

        view, request = self._real_view_and_request()
        self._seed_ports(view, device, [{"port_id": 1, "ifName": "Gi0/0", "ifAdminStatus": "up", "ifAlias": None}])

        with patch(
            "netbox_librenms_plugin.librenms_api.build_librenms_api",
            return_value=MagicMock(server_key="default"),
        ):
            ctx = view.get_context_data(request, device, "ifName")

        gi01 = next((i for i in ctx["netbox_only_interfaces"] if i["name"] == "Gi0/1"), None)
        assert gi01 is not None
        assert gi01["device_name"] == "router-nbonly"


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
        """A malformed-but-dict-shaped payload (e.g. a non-string ip value) resolves to an empty string."""
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

        # post() resolves through restrict_object_or_404, not get_object: patching the latter
        # made this assertion vacuous.
        with patch.object(view, "restrict_object_or_404") as mock_resolve:
            response = view.post(request)

        assert response.status_code == 403
        mock_resolve.assert_not_called()  # device never resolved → no arbitrary-ID probing


# ---------------------------------------------------------------------------
# DeviceCableTableView.get_links_data — coerces the cached librenms_id (real DB)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestCableLinksDataCoercesLibreNMSId:
    """get_links_data() must coerce whatever get_librenms_id() hands back before fetching links.

    The id-cache path returns its value verbatim (the custom-field/discovery paths already
    coerce), so a poisoned cache holding ``True`` reaches the cables view as a truthy non-int
    that ``int(True)`` would silently turn into device id ``1`` — fetching a stranger's links
    and ports. The view must fail closed on it BEFORE get_device_links()/get_ports(), mirroring
    the interfaces-POST contract (TestBaseInterfaceTablePostCoercesLibreNMSId).
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

    def test_corrupt_cached_id_fails_closed_before_link_and_port_fetch(self):
        from django.core.cache import cache as real_cache

        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        device = make_device("coerce-cables-host")  # no librenms_id custom field → forces the cache path
        api = self._real_api()
        # Poison the device-id cache — the ONE get_librenms_id path that does not coerce.
        real_cache.set(api._get_cache_key(device), True)
        try:
            # The real lookup hands back the uncoerced bool, so this isn't a straw-man mock return.
            assert api.get_librenms_id(device) is True

            view = object.__new__(DeviceCableTableView)
            view._librenms_api = api
            # get_device_links / get_ports are the external HTTP boundary; spy to prove neither is
            # reached with the poisoned id. int(True) == 1 would otherwise GET /devices/1/links.
            api.get_device_links = MagicMock(name="get_device_links", return_value=(False, "should-not-be-called"))
            api.get_ports = MagicMock(name="get_ports", return_value=(False, "should-not-be-called"))

            result = view.get_links_data(device, server_key="default")
        finally:
            real_cache.delete(api._get_cache_key(device))

        # Coerced to None → fail closed: the host link/port fetches are skipped entirely (no OOB
        # mapping on this device, so neither spy fires at all), and the unmapped device resolves
        # to a fetch failure (None) rather than fetching device 1's data.
        assert view.librenms_id is None
        api.get_device_links.assert_not_called()
        api.get_ports.assert_not_called()
        assert result is None


@pytest.mark.django_db
class TestIPSyncFetchFailureKeepsMoveCard:
    """On a LibreNMS fetch failure the IP-sync error re-render still surfaces movable_ips.

    The per-row "Move IP addresses to <winner>" moves are pure NetBox operations, so a migrated
    donor must keep the move card when LibreNMS is briefly unreachable — the card is gated on
    ip_sync.movable_ips, which every other exit provides.
    """

    def _view(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        view = object.__new__(DeviceIPAddressTableView)
        view._librenms_api = MagicMock(server_key="default")
        return view

    def test_fetch_failure_context_includes_movable_ips(self):
        from django.http import HttpResponse
        from django.test import RequestFactory

        donor = make_device("ipsync-donor-mig")
        # Migrated donor: _migrated_to marker under the server sub-block, no live id.
        donor.custom_field_data["librenms_id"] = {
            "default": {"_migrated_to": {"device_id": 7, "server_key": "default"}}
        }
        donor.save()
        iface = make_interface(donor, "Gi0/1")
        make_ip("10.0.0.5/24", assigned_object=iface)

        view = self._view()
        request = RequestFactory().post("/x/", data={"server_key": "default"})

        captured = {}

        def fake_render(req, obj, server_key, ctx):
            captured["ctx"] = ctx
            return HttpResponse("x")

        with (
            patch.object(view, "get_object", return_value=donor),
            patch.object(view, "rebind_api_for_server", return_value="default"),
            # A valid server whose live IP fetch fails → _prepare_context(fetch_fresh=True) is None.
            patch.object(view, "_prepare_context", return_value=None),
            patch.object(view, "render_sync_partial", side_effect=fake_render),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.messages"),
        ):
            view.post(request, pk=donor.pk)

        ip_sync = captured["ctx"]["ip_sync"]
        assert ip_sync["movable_ips"], "Move-IP card context lost on fetch failure"
        assert any(m["address"].startswith("10.0.0.5") for m in ip_sync["movable_ips"])


@pytest.mark.django_db
class TestIPSyncRebindFailureKeepsMoveCard:
    """On a stale/unconfigured POSTed server_key the IP-sync rebind-failure re-render must still surface movable_ips — the same move card the fetch-failure and success branches keep.

    The card is gated on ip_sync.movable_ips; omitting it (as this branch did) made a migrated
    donor's "Move IP addresses to <winner>" card vanish whenever the POSTed server_key went stale.
    """

    def _view(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        view = object.__new__(DeviceIPAddressTableView)
        # active_server_key resolves from the cached client; the branch renders against it.
        view._librenms_api = MagicMock(server_key="default")
        return view

    def test_rebind_failure_context_includes_movable_ips(self):
        from django.http import HttpResponse
        from django.test import RequestFactory

        donor = make_device("ipsync-rebind-donor")
        # Migrated donor: _migrated_to marker under the "default" sub-block, no live id.
        donor.custom_field_data["librenms_id"] = {
            "default": {"_migrated_to": {"device_id": 7, "server_key": "default"}}
        }
        donor.save()
        iface = make_interface(donor, "Gi0/1")
        make_ip("10.0.0.5/24", assigned_object=iface)

        view = self._view()
        request = RequestFactory().post("/x/", data={"server_key": "ghost-unconfigured"})

        captured = {}

        def fake_render(req, obj, server_key, ctx):
            captured["ctx"] = ctx
            captured["server_key"] = server_key
            return HttpResponse("x")

        with (
            patch.object(view, "get_object", return_value=donor),
            # A stale/unconfigured POSTed key → rebind returns None → the rebind-failure branch runs.
            patch.object(view, "rebind_api_for_server", return_value=None),
            patch.object(view, "render_sync_partial", side_effect=fake_render),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.messages"),
        ):
            view.post(request, pk=donor.pk)

        # The branch renders migrated context + movable_ips against active_server_key ("default"),
        # not the known-invalid POSTed key (which is not echoed back into ip_sync.server_key).
        assert captured["server_key"] == "default"
        ip_sync = captured["ctx"]["ip_sync"]
        assert ip_sync["server_key"] is None
        assert ip_sync["movable_ips"], "Move-IP card context lost on rebind failure"
        assert any(m["address"].startswith("10.0.0.5") for m in ip_sync["movable_ips"])


@pytest.mark.django_db
class TestRenderSyncPartialInjectsWritePermission:
    """render_sync_partial injects has_write_permission from the request user at the shared chokepoint, so a migrated donor's 'Move to winner' controls render on every HTMX re-render, not just a full page reload.

    Only modules_view (which renders directly) passed the flag; the interface/IP branches route
    through render_sync_partial and omitted it, silently collapsing every move button to the
    disabled read-only branch even for a user with change permission.
    """

    def _ip_view(self, *, superuser):
        from core.models import ObjectType
        from django.apps import apps
        from django.contrib.auth import get_user_model
        from django.test import RequestFactory
        from users.models import ObjectPermission

        from netbox_librenms_plugin.tests.conftest import make_superuser
        from netbox_librenms_plugin.views.object_sync.devices import DeviceIPAddressTableView

        view = object.__new__(DeviceIPAddressTableView)
        view._librenms_api = MagicMock(server_key="default")
        request = RequestFactory().post("/x/")
        if superuser:
            request.user = make_superuser()
        else:
            settings_model = apps.get_model("netbox_librenms_plugin", "LibreNMSSettings")
            user = get_user_model().objects.create_user(username="rsp-perm-viewer", password="x")
            view_permission = ObjectPermission.objects.create(name="rsp-perm-plugin-view", actions=["view"])
            view_permission.object_types.set([ObjectType.objects.get_for_model(settings_model)])
            view_permission.users.set([user])
            request.user = get_user_model().objects.get(pk=user.pk)
        view.request = request
        return view, request

    def _migrated_donor_with_movable_ip(self):
        from netbox_librenms_plugin.tests.conftest import ip_on
        from netbox_librenms_plugin.utils import mark_librenms_migrated

        winner = make_device("rsp-perm-winner")
        donor = make_device("rsp-perm-donor")
        ip = ip_on(donor, "10.0.0.5/24", "eth0")  # IP on a donor interface → a move-to-winner candidate
        mark_librenms_migrated(donor, winner.pk, "default")
        donor.save()
        return donor, winner, ip

    def _ip_sync_ctx(self, donor, ip):
        """Build the ip_sync context WITHOUT has_write_permission, exactly as the view branches do."""
        from django.test import RequestFactory
        from django_tables2 import RequestConfig

        from netbox_librenms_plugin.tables.ipaddresses import IPAddressTable

        table = IPAddressTable([])
        RequestConfig(RequestFactory().get("/")).configure(table)
        return {
            "ip_sync": {
                "object": donor,
                "table": table,
                "server_key": "default",
                "set_primary_ip": False,
                "cache_expiry": None,
                "movable_ips": [{"id": ip.pk, "address": "10.0.0.5/24", "interface_name": "eth0"}],
            },
        }

    def test_write_permitted_user_gets_a_live_move_button(self):
        from django.urls import reverse

        view, request = self._ip_view(superuser=True)
        donor, _winner, ip = self._migrated_donor_with_movable_ip()
        resp = view.render_sync_partial(request, donor, "default", self._ip_sync_ctx(donor, ip))
        html = resp.content.decode()
        move_url = reverse("plugins:netbox_librenms_plugin:ipaddress_move_to_winner", kwargs={"pk": ip.pk})
        # has_write_permission=True (injected by the chokepoint) reached _migrate_move_button.html.
        assert move_url in html
        assert "read-only" not in html

    def test_read_only_user_gets_read_only_text_not_a_button(self):
        from django.urls import reverse

        from netbox_librenms_plugin.constants import PERM_CHANGE_PLUGIN, PERM_VIEW_PLUGIN

        view, request = self._ip_view(superuser=False)
        assert request.user.has_perm(PERM_VIEW_PLUGIN)
        assert not request.user.has_perm(PERM_CHANGE_PLUGIN)
        donor, _winner, ip = self._migrated_donor_with_movable_ip()
        resp = view.render_sync_partial(request, donor, "default", self._ip_sync_ctx(donor, ip))
        html = resp.content.decode()
        move_url = reverse("plugins:netbox_librenms_plugin:ipaddress_move_to_winner", kwargs={"pk": ip.pk})
        # A genuine view-only plugin user gets muted text, not a live mutating button.
        assert "read-only" in html
        assert move_url not in html


@pytest.mark.django_db
class TestVCInterfaceRenderMemberResolutionNotPerPort:
    """The VC interface render resolves the owning member from a prebuilt map, not a query per port."""

    def _view(self):
        from netbox_librenms_plugin.views.object_sync.devices import DeviceInterfaceTableView

        view = object.__new__(DeviceInterfaceTableView)
        view._librenms_api = MagicMock(server_key="default", cache_timeout=300)
        return view

    def test_render_query_count_invariant_to_port_count(self):
        """Rendering 2 vs 8 cached ports issues the SAME number of queries (no per-port member lookup).

        get_virtual_chassis_member was the only per-port DB query in the cached-render loop; passing
        the prebuilt {vc_position: member} map makes it O(1), so the query count no longer scales
        with the number of ports.
        """
        from dcim.models import Interface
        from django.core.cache import cache as dj_cache
        from django.db import connection
        from django.test import RequestFactory
        from django.test.utils import CaptureQueriesContext

        from netbox_librenms_plugin.tests.conftest import make_device, make_interface, make_virtual_chassis

        m1 = make_device("vc-if-m1")
        m2 = make_device("vc-if-m2")
        make_virtual_chassis("vc-if-render", m1, m2)
        make_interface(m1, "Gi1/0/1")
        make_interface(m2, "Gi2/0/1")
        obj = m1  # viewed member; obj.virtual_chassis is the VC

        view = self._view()
        request = RequestFactory().get("/x/")
        request.user = make_user_with_perms(
            "vc-if-render-viewer",
            [("view", Device), ("view", Interface)],
        )
        view.request = request
        cache_key = view.get_cache_key(obj, "ports", "default")

        def render(nports):
            ports = [{"port_id": i, "ifName": f"Gi1/0/{i}", "ifType": "ethernetCsmacd"} for i in range(1, nports + 1)]
            dj_cache.set(cache_key, {"ports": ports})
            with CaptureQueriesContext(connection) as ctx:
                context = view.get_context_data(request, obj, "ifName", server_key="default", sync_device=obj)
            return len(ctx.captured_queries), list(context["table"].data)

        try:
            # Warm process-level caches (ContentType etc.) first so the two measured renders differ
            # ONLY by port count, not by cold-cache one-time queries on the first call.
            render(3)
            q_small, small_rows = render(2)
            q_large, large_rows = render(8)
            assert any(row.get("netbox_interface") is not None for row in small_rows)
            assert any(row.get("netbox_interface") is not None for row in large_rows)
            assert q_large == q_small, f"query count scaled with ports: {q_small} -> {q_large} (per-port N+1)"
        finally:
            # Django's cache isn't wrapped in the test's DB-transaction rollback, so delete the
            # entry (like the sibling cache-based tests) to avoid leaking it into later runs.
            dj_cache.delete(cache_key)


@pytest.mark.django_db
class TestEnrichPortLagParentNameFallback:
    """Relationship name fallback must honor the user-selected name field.

    The displayed LAG/parent name comes from port.get(interface_name_field) and
    The relationship signal already scans {ifName, ifDescr, interface_name_field}; the
    match fallback (no stored librenms_id on either side) compared ifName/ifDescr
    only, so an ifAlias-driven deployment misreported a genuine match as mismatch.
    """

    def test_lag_match_via_interface_name_field_alias(self):
        from netbox_librenms_plugin.interface_relationships import RelationshipMaps, enrich_port_relationships

        dev = make_device("lag-alias-host")
        agg = make_interface(dev, "CUSTOMER-UPLINK-A", iface_type="lag")  # named from ifAlias
        member = make_interface(dev, "Gi0/1")
        member.lag = agg
        member.save()

        agg_port = {"port_id": 10, "ifName": "Po1", "ifDescr": "Port-channel1", "ifAlias": "CUSTOMER-UPLINK-A"}
        port = {"port_id": 1, "netbox_interface": member}

        enrich_port_relationships(
            port,
            RelationshipMaps({1: 10}, {}, {10: agg_port}),
            interface_name_field="ifAlias",
        )

        assert port["librenms_lag_name"] == "CUSTOMER-UPLINK-A"
        assert port["lag_sync_status"] == "match"

    def test_parent_match_via_interface_name_field_alias(self):
        from netbox_librenms_plugin.interface_relationships import RelationshipMaps, enrich_port_relationships

        dev = make_device("parent-alias-host")
        parent = make_interface(dev, "CORE-TRUNK-B")  # named from ifAlias
        child = make_interface(dev, "Gi0/2.100")
        child.parent = parent
        child.save()

        parent_port = {"port_id": 20, "ifName": "Gi0/2", "ifDescr": "GigabitEthernet0/2", "ifAlias": "CORE-TRUNK-B"}
        port = {"port_id": 2, "netbox_interface": child}

        enrich_port_relationships(
            port,
            RelationshipMaps({}, {2: 20}, {20: parent_port}),
            interface_name_field="ifAlias",
        )

        assert port["librenms_parent_name"] == "CORE-TRUNK-B"
        assert port["parent_sync_status"] == "match"


@pytest.mark.django_db
class TestEnrichPortLagParentStatusSymmetry:
    """LAG and parent must classify sync status identically across all 5 branches (the invariant the shared helper guarantees)."""

    def _status(self, kind, *, lnms_has_rel, nb_related, n):
        """Enrich a fresh port for ONE relationship kind and return its sync status.

        kind: "lag" | "parent". nb_related: None | "matching" | "nonmatching".
        """
        from netbox_librenms_plugin.interface_relationships import RelationshipMaps, enrich_port_relationships
        from netbox_librenms_plugin.utils import set_librenms_device_id

        dev = make_device(f"rel-sym-{kind}-{n}")
        child = make_interface(dev, f"child-{kind}-{n}")
        child_pid = 1000 + n
        set_librenms_device_id(child, child_pid, "default")
        rel_lnms_port_id = 500 + n

        by_id = {}
        if nb_related is not None:
            related = make_interface(dev, f"rel-{kind}-{n}", iface_type="lag" if kind == "lag" else "other")
            if nb_related == "matching":
                set_librenms_device_id(related, rel_lnms_port_id, "default")
                related.save()
            setattr(child, "lag" if kind == "lag" else "parent", related)
        child.save()

        rel_map = {}
        if lnms_has_rel:
            # ifName intentionally "rel-<kind>-<n>" so a "matching" related iface (same name) matches
            # by name too, and a "nonmatching" one (named "other-...") fails both id and name.
            by_id[rel_lnms_port_id] = {"port_id": rel_lnms_port_id, "ifName": f"rel-{kind}-{n}"}
            rel_map = {child_pid: rel_lnms_port_id}
            if nb_related == "nonmatching":
                related.name = f"other-{kind}-{n}"
                related.save()

        port = {"port_id": child_pid, "netbox_interface": child}
        enrich_port_relationships(
            port,
            RelationshipMaps(
                rel_map if kind == "lag" else {},
                rel_map if kind == "parent" else {},
                by_id,
            ),
            interface_name_field="ifName",
        )
        return port["lag_sync_status" if kind == "lag" else "parent_sync_status"]

    def test_all_status_branches_are_symmetric(self):
        # (lnms_has_rel, nb_related) -> expected status; run through BOTH relationship kinds.
        scenarios = [
            (True, "matching", "match"),
            (True, "nonmatching", "mismatch"),
            (True, None, "missing_nb"),
            (False, "matching", "missing_lnms"),  # NetBox has the related iface, LibreNMS doesn't
            (False, None, None),
        ]
        for i, (has_rel, nb_rel, expected) in enumerate(scenarios):
            lag = self._status("lag", lnms_has_rel=has_rel, nb_related=nb_rel, n=i)
            parent = self._status("parent", lnms_has_rel=has_rel, nb_related=nb_rel, n=100 + i)
            assert lag == parent == expected, f"{(has_rel, nb_rel)}: lag={lag} parent={parent} expected={expected}"
