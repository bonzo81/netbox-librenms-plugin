"""
Additional coverage tests for:
  - views/base/cables_view.py   (currently ~59%)
  - views/base/ip_addresses_view.py (~62%)

All tests follow strict project conventions:
  - Plain pytest classes, NO @pytest.mark.django_db
  - Mock ALL database interactions with MagicMock
  - Inline imports inside test methods
  - assert x == y style
  - Use object.__new__(ClassName) to bypass __init__
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
    obj.virtual_chassis = None
    return obj


def _mock_request(path="/plugins/librenms/device/1/cables/"):
    req = MagicMock()
    req.path = path
    req.GET = {}
    req.POST = {}
    req.headers = {}
    return req


# =============================================================================
# TestLibreNMSIdQ  — _librenms_id_q edge cases
# =============================================================================


class TestLibreNMSIdQ:
    """Tests for _librenms_id_q edge cases (lines 29-43)."""

    def test_bool_true_returns_match_nothing_q(self):
        """Boolean True → Q(pk__isnull=True) & Q(pk__isnull=False) (matches nothing)."""
        from netbox_librenms_plugin.views.base.cables_view import _librenms_id_q
        from django.db.models import Q

        result = _librenms_id_q("default", True)
        expected = Q(pk__isnull=True) & Q(pk__isnull=False)
        assert str(result) == str(expected)

    def test_bool_false_returns_match_nothing_q(self):
        """Boolean False also returns match-nothing Q."""
        from netbox_librenms_plugin.views.base.cables_view import _librenms_id_q
        from django.db.models import Q

        result = _librenms_id_q("default", False)
        expected = Q(pk__isnull=True) & Q(pk__isnull=False)
        assert str(result) == str(expected)

    def test_string_int_value_adds_integer_variant(self):
        """String '10': int_val=10 != '10' (value is str) → adds integer variants (lines 37-38)."""
        from netbox_librenms_plugin.views.base.cables_view import _librenms_id_q
        from django.db.models import Q

        result = _librenms_id_q("default", "10")
        # The Q should include both the string "10" form and the integer 10 form.
        # Verify the result is a compound Q that references integer 10.
        result_str = str(result)
        assert "10" in result_str
        # It should NOT just be the base Q; confirm the extra integer variant was added
        base_only = Q(custom_field_data__librenms_id__default="10") | Q(custom_field_data__librenms_id="10")
        assert str(result) != str(base_only)

    def test_int_value_adds_string_variant(self):
        """Integer 10: str_val='10' != 10 (value is int) → adds string variants (lines 39-41)."""
        from netbox_librenms_plugin.views.base.cables_view import _librenms_id_q
        from django.db.models import Q

        result = _librenms_id_q("default", 10)
        result_str = str(result)
        assert "10" in result_str
        # Confirm the extra string variant was added
        base_only = Q(custom_field_data__librenms_id__default=10) | Q(custom_field_data__librenms_id=10)
        assert str(result) != str(base_only)

    def test_non_int_string_value_except_caught(self):
        """Non-convertible string 'abc' → ValueError caught, base Q returned (lines 42-43)."""
        from netbox_librenms_plugin.views.base.cables_view import _librenms_id_q

        # Should NOT raise — the except catches ValueError
        result = _librenms_id_q("default", "abc")
        assert result is not None

    def test_none_value_typeerror_caught(self):
        """None → TypeError on int(None) caught, base Q returned (lines 42-43)."""
        from netbox_librenms_plugin.views.base.cables_view import _librenms_id_q

        result = _librenms_id_q("default", None)
        assert result is not None


# =============================================================================
# TestGetObjectAndIpAddress  — BaseCableTableView trivial wrappers
# =============================================================================


class TestGetObjectAndIpAddress:
    """Tests for BaseCableTableView.get_object (line 57) and get_ip_address (lines 61-63)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view.model = MagicMock()
        return view

    def test_get_object_calls_get_object_or_404(self):
        """get_object calls get_object_or_404 with view.model and given pk."""
        view = self._make_view()
        mock_device = MagicMock()

        with patch(
            "netbox_librenms_plugin.views.base.cables_view.get_object_or_404",
            return_value=mock_device,
        ) as mock_get:
            result = view.get_object(42)

        mock_get.assert_called_once_with(view.model, pk=42)
        assert result is mock_device

    def test_get_ip_address_with_primary_ip(self):
        """get_ip_address returns the string representation of primary_ip when present."""
        view = self._make_view()
        obj = MagicMock()
        obj.primary_ip.address.ip = "192.168.1.1"

        result = view.get_ip_address(obj)
        assert result == "192.168.1.1"

    def test_get_ip_address_without_primary_ip(self):
        """get_ip_address returns None when obj has no primary_ip."""
        view = self._make_view()
        obj = MagicMock()
        obj.primary_ip = None

        result = view.get_ip_address(obj)
        assert result is None


# =============================================================================
# TestGetPortsDataFailure  — get_ports_data failure path (line 73)
# =============================================================================


class TestGetPortsDataFailure:
    """Tests for BaseCableTableView.get_ports_data failure path."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view.librenms_id = 42
        return view

    def test_returns_empty_ports_on_api_failure(self):
        """When librenms_api.get_ports() returns failure, returns {'ports': []}."""
        view = self._make_view()
        view._librenms_api.get_ports.return_value = (False, {})

        obj = _mock_obj()

        with patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache:
            mock_cache.get.return_value = None
            with patch.object(view, "get_cache_key", return_value="test-key"):
                result = view.get_ports_data(obj)

        assert result == {"ports": []}

    def test_returns_cached_data_without_api_call(self):
        """When cached data exists, returns it without hitting the API."""
        view = self._make_view()
        cached = {"ports": [{"port_id": 1, "ifName": "Gi0/0"}]}

        obj = _mock_obj()

        with patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache:
            mock_cache.get.return_value = cached
            with patch.object(view, "get_cache_key", return_value="test-key"):
                result = view.get_ports_data(obj)

        assert result is cached
        view._librenms_api.get_ports.assert_not_called()


# =============================================================================
# TestGetLinksDataPortNameNone  — continue branch when port_name is None (line 98)
# =============================================================================


class TestGetLinksDataPortNameNone:
    """Tests for get_links_data when port.get(interface_name_field) is None → skipped."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view.request = _mock_request()
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view.librenms_id = 42
        return view

    def test_port_name_none_excluded_from_local_ports_map(self):
        """Port with None for interface_name_field is skipped; local_port maps to None."""
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
        view._librenms_api.get_librenms_id.return_value = 42

        ports_data = {
            "ports": [
                {"port_id": 10, "ifName": None},  # port_name is None → continue
            ]
        }

        obj = _mock_obj()

        with (
            patch.object(view, "get_ports_data", return_value=ports_data),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_interface_name_field",
                return_value="ifName",
            ),
        ):
            result = view.get_links_data(obj)

        assert result is not None
        # local_port is None because port_id=10 was skipped from the map
        assert result[0]["local_port"] is None


# =============================================================================
# TestGetDeviceByIdOrNameEdgeCases  — MultipleObjectsReturned, FQDN fallback
# =============================================================================


class TestGetDeviceByIdOrNameEdgeCases:
    """Tests for get_device_by_id_or_name edge cases (lines 123-126, 144-145)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_multiple_objects_returned_for_librenms_id(self):
        """MultipleObjectsReturned on librenms_id → (None, False, error message)."""
        from django.core.exceptions import MultipleObjectsReturned

        view = self._make_view()

        with patch("netbox_librenms_plugin.views.base.cables_view.Device") as MockDevice:
            # Use a narrow DoesNotExist so it doesn't swallow MultipleObjectsReturned
            class _DoesNotExist(Exception):
                pass

            MockDevice.DoesNotExist = _DoesNotExist
            MockDevice.objects.get.side_effect = MultipleObjectsReturned

            device, found, error = view.get_device_by_id_or_name(42, "switch.example.com")

        assert device is None
        assert found is False
        assert error is not None
        assert "42" in error

    def test_fqdn_fails_simple_hostname_succeeds(self):
        """FQDN lookup raises DoesNotExist; short hostname lookup succeeds (lines 144-145)."""
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"

        mock_device = MagicMock()

        with patch("netbox_librenms_plugin.views.base.cables_view.Device") as MockDevice:

            class _DoesNotExist(Exception):
                pass

            MockDevice.DoesNotExist = _DoesNotExist
            # remote_device_id=None → skip librenms_id lookup, go straight to name
            # First get() (FQDN "switch.example.com") raises DoesNotExist
            # Second get() (simple "switch") succeeds
            MockDevice.objects.get.side_effect = [_DoesNotExist, mock_device]

            device, found, error = view.get_device_by_id_or_name(None, "switch.example.com")

        assert found is True
        assert device is mock_device
        assert error is None


# =============================================================================
# TestEnrichLocalPortVC  — VC member path in enrich_local_port (line 174)
# =============================================================================


class TestEnrichLocalPortVC:
    """Tests for enrich_local_port when obj.virtual_chassis is truthy."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_vc_path_calls_get_virtual_chassis_member(self):
        """VC device → get_virtual_chassis_member called; interface URL set."""
        view = self._make_view()

        obj = MagicMock()
        obj.virtual_chassis = MagicMock()  # truthy

        mock_interface = MagicMock()
        mock_interface.pk = 99

        mock_member = MagicMock()
        mock_member.interfaces.filter.return_value.first.return_value = mock_interface

        link = {"local_port": "Gi0/0", "local_port_id": 10}

        with (
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_virtual_chassis_member",
                return_value=mock_member,
            ) as mock_vc,
            patch(
                "netbox_librenms_plugin.views.base.cables_view.reverse",
                return_value="/dcim/interfaces/99/",
            ),
        ):
            view.enrich_local_port(link, obj)

        mock_vc.assert_called_once_with(obj, "Gi0/0")
        assert link.get("local_port_url") == "/dcim/interfaces/99/"
        assert link.get("netbox_local_interface_id") == 99


# =============================================================================
# TestEnrichRemotePort  — VC and non-VC paths (lines 190-227)
# =============================================================================


class TestEnrichRemotePort:
    """Tests for enrich_remote_port VC and non-VC paths."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_vc_path_finds_by_librenms_id(self):
        """VC device: remote interface found by librenms_id; URL/name/id set."""
        view = self._make_view()

        device = MagicMock()
        device.virtual_chassis = MagicMock()  # truthy

        mock_interface = MagicMock()
        mock_interface.pk = 77
        mock_interface.name = "Gi1/0/1"

        mock_member = MagicMock()
        mock_member.interfaces.filter.return_value.first.return_value = mock_interface

        link = {"remote_port": "Gi1/0/1", "remote_port_id": 20}

        with (
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_virtual_chassis_member",
                return_value=mock_member,
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.reverse",
                return_value="/dcim/interfaces/77/",
            ),
        ):
            result = view.enrich_remote_port(link, device)

        assert result["netbox_remote_interface_id"] == 77
        assert result["remote_port_url"] == "/dcim/interfaces/77/"
        assert result["remote_port_name"] == "Gi1/0/1"

    def test_vc_path_falls_back_to_name_when_librenms_id_miss(self):
        """VC device: librenms_id lookup returns None → falls back to name match."""
        view = self._make_view()

        device = MagicMock()
        device.virtual_chassis = MagicMock()  # truthy

        mock_interface = MagicMock()
        mock_interface.pk = 55
        mock_interface.name = "Gi1/0/2"

        mock_member = MagicMock()
        # remote_port_id=20 (truthy) → librenms_id filter called (returns None),
        # then name filter called (returns interface)
        mock_member.interfaces.filter.return_value.first.side_effect = [None, mock_interface]

        link = {"remote_port": "Gi1/0/2", "remote_port_id": 20}

        with (
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_virtual_chassis_member",
                return_value=mock_member,
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.reverse",
                return_value="/dcim/interfaces/55/",
            ),
        ):
            result = view.enrich_remote_port(link, device)

        assert result["netbox_remote_interface_id"] == 55

    def test_non_vc_path_finds_by_librenms_id(self):
        """Non-VC device: remote interface found by librenms_id; URL/name/id set."""
        view = self._make_view()

        device = MagicMock()
        device.virtual_chassis = None  # falsy

        mock_interface = MagicMock()
        mock_interface.pk = 33
        mock_interface.name = "eth0"

        device.interfaces.filter.return_value.first.return_value = mock_interface

        link = {"remote_port": "eth0", "remote_port_id": 15}

        with patch(
            "netbox_librenms_plugin.views.base.cables_view.reverse",
            return_value="/dcim/interfaces/33/",
        ):
            result = view.enrich_remote_port(link, device)

        assert result["netbox_remote_interface_id"] == 33
        assert result["remote_port_url"] == "/dcim/interfaces/33/"
        assert result["remote_port_name"] == "eth0"

    def test_non_vc_path_falls_back_to_name(self):
        """Non-VC device: librenms_id lookup returns None → falls back to name match."""
        view = self._make_view()

        device = MagicMock()
        device.virtual_chassis = None  # falsy

        mock_interface = MagicMock()
        mock_interface.pk = 44
        mock_interface.name = "eth1"

        # remote_port_id=15 (truthy) → librenms_id filter called first (returns None),
        # then name filter called (returns interface)
        device.interfaces.filter.return_value.first.side_effect = [None, mock_interface]

        link = {"remote_port": "eth1", "remote_port_id": 15}

        with patch(
            "netbox_librenms_plugin.views.base.cables_view.reverse",
            return_value="/dcim/interfaces/44/",
        ):
            result = view.enrich_remote_port(link, device)

        assert result["netbox_remote_interface_id"] == 44

    def test_no_remote_port_key_returns_none(self):
        """When link has no 'remote_port', method falls through and returns None."""
        view = self._make_view()
        device = MagicMock()
        link = {}  # No remote_port key

        result = view.enrich_remote_port(link, device)
        assert result is None

    def test_interface_not_found_does_not_set_url(self):
        """When no remote interface found, url/id keys are not set."""
        view = self._make_view()

        device = MagicMock()
        device.virtual_chassis = None

        # Both lookups return None
        device.interfaces.filter.return_value.first.return_value = None

        link = {"remote_port": "eth2", "remote_port_id": 99}

        result = view.enrich_remote_port(link, device)

        assert "remote_port_url" not in result
        assert "netbox_remote_interface_id" not in result


# =============================================================================
# TestProcessRemoteDevice  — found=True and found=False paths (lines 264-283)
# =============================================================================


class TestProcessRemoteDevice:
    """Tests for process_remote_device found=True and found=False paths."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_found_true_sets_remote_device_url_and_calls_enrich(self):
        """found=True → sets remote_device_url, netbox_remote_device_id, calls enrich_remote_port."""
        view = self._make_view()

        mock_device = MagicMock()
        mock_device.pk = 5

        link = {"remote_port": "Gi0/1", "remote_port_id": None}

        with (
            patch.object(view, "get_device_by_id_or_name", return_value=(mock_device, True, None)),
            patch.object(
                view, "enrich_remote_port", side_effect=lambda link, *_args, **_kwargs: dict(link)
            ) as mock_enrich,
            patch(
                "netbox_librenms_plugin.views.base.cables_view.reverse",
                return_value="/dcim/devices/5/",
            ),
        ):
            result = view.process_remote_device(link, "switch-b", 99)

        assert result["remote_device_url"] == "/dcim/devices/5/"
        assert result["netbox_remote_device_id"] == 5
        mock_enrich.assert_called_once()

    def test_found_false_with_error_message(self):
        """found=False with error_message → cable_status set to the error."""
        view = self._make_view()

        link = {"remote_port": "Gi0/1", "remote_port_id": None}

        with patch.object(
            view,
            "get_device_by_id_or_name",
            return_value=(None, False, "Multiple devices found: 99"),
        ):
            result = view.process_remote_device(link, "switch-b", 99)

        assert result["cable_status"] == "Multiple devices found: 99"
        assert result["can_create_cable"] is False

    def test_found_false_without_error_message_uses_default(self):
        """found=False, error_message=None → cable_status = 'Device Not Found in NetBox'."""
        view = self._make_view()

        link = {"remote_port": "Gi0/1", "remote_port_id": None}

        with patch.object(view, "get_device_by_id_or_name", return_value=(None, False, None)):
            result = view.process_remote_device(link, "switch-b", None)

        assert result["cable_status"] == "Device Not Found in NetBox"
        assert result["can_create_cable"] is False


# =============================================================================
# TestGetTableOverride  — BaseCableTableView.get_table (lines 302-305)
# =============================================================================


class TestGetTableOverride:
    """Tests for BaseCableTableView.get_table — sets htmx_url after calling super()."""

    def _make_testable_view(self, server_key="default", path="/cables/"):
        """Create a testable subclass that injects a concrete get_table via MRO."""
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        mock_table = MagicMock()

        class _FakeParent:
            def get_table(self, data, obj):
                return mock_table

        class _TestableCableView(BaseCableTableView, _FakeParent):
            pass

        view = object.__new__(_TestableCableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = server_key
        view.request = _mock_request(path)
        return view, mock_table

    def test_sets_htmx_url_with_server_key(self):
        """get_table sets htmx_url including server_key when present."""
        view, mock_table = self._make_testable_view(server_key="default", path="/cables/")

        result = view.get_table([], MagicMock())

        assert result is mock_table
        assert result.htmx_url == "/cables/?tab=cables&server_key=default"

    def test_htmx_url_without_server_key(self):
        """When server_key is falsy, htmx_url has no server_key parameter."""
        view, mock_table = self._make_testable_view(server_key=None, path="/cables/")

        result = view.get_table([], MagicMock())

        assert result.htmx_url == "/cables/?tab=cables"


# =============================================================================
# TestPostHandlerVC  — SingleCableVerifyView.post() VC resolution (line 471)
# =============================================================================


class TestPostHandlerVC:
    """Tests for SingleCableVerifyView.post() VC member resolution path."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        view = object.__new__(SingleCableVerifyView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_vc_member_resolution_calls_get_virtual_chassis_member(self):
        """VC device → get_virtual_chassis_member called with device and local_port."""
        import json

        view = self._make_view()

        mock_request = MagicMock()
        mock_request.body = json.dumps(
            {
                "device_id": 1,
                "local_port_id": 10,
                "server_key": "default",
            }
        ).encode()

        mock_device = MagicMock()
        mock_device.virtual_chassis = MagicMock()  # truthy
        mock_device.id = 1

        mock_member = MagicMock()
        mock_interface = MagicMock()
        mock_interface.pk = 99
        # librenms_id lookup returns the interface
        mock_member.interfaces.filter.return_value.first.return_value = mock_interface

        cached_links = {
            "links": [
                {
                    "local_port_id": 10,
                    "local_port": "Gi0/0",
                    "remote_port": "Gi0/1",
                    "remote_device": "switch-b",
                    "remote_port_id": 20,
                    "remote_device_id": 99,
                }
            ]
        }

        with (
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_object_or_404",
                return_value=mock_device,
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device",
                return_value=mock_device,
            ),
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="test-key"),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_virtual_chassis_member",
                return_value=mock_member,
            ) as mock_vc,
            patch.object(
                view,
                "process_remote_device",
                return_value={
                    "local_port": "Gi0/0",
                    "remote_port": "Gi0/1",
                    "remote_device": "switch-b",
                    "remote_port_id": 20,
                    "remote_device_id": 99,
                },
            ) as mock_process_remote,
            patch(
                "netbox_librenms_plugin.views.base.cables_view.reverse",
                return_value="/interface/99/",
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_token",
                return_value="csrf-token",
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.escape",
                side_effect=lambda x: x,
            ),
        ):
            mock_cache.get.return_value = cached_links
            view.post(mock_request)

        mock_vc.assert_called_once_with(mock_device, "Gi0/0")
        # Verify server_key is forwarded to process_remote_device
        assert mock_process_remote.called
        call_kwargs = mock_process_remote.call_args[1]
        assert call_kwargs.get("server_key") == "default"


# =============================================================================
# TestPostHandlerInterfaceNotFound  — lines 534-561
# =============================================================================


class TestPostHandlerInterfaceNotFound:
    """Tests for SingleCableVerifyView.post() interface-not-found and cable_url branches."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        view = object.__new__(SingleCableVerifyView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_interface_not_found_fills_formatted_row(self):
        """When no local interface found, formatted_row reflects missing interface."""
        import json as json_mod

        view = self._make_view()

        mock_request = MagicMock()
        mock_request.body = json_mod.dumps(
            {
                "device_id": 1,
                "local_port_id": 10,
                "server_key": "default",
            }
        ).encode()

        mock_device = MagicMock()
        mock_device.virtual_chassis = None  # non-VC
        mock_device.id = 1
        # Both interface lookups return None
        mock_device.interfaces.filter.return_value.first.return_value = None

        cached_links = {
            "links": [
                {
                    "local_port_id": 10,
                    "local_port": "Gi0/0",
                    "remote_port": "Gi0/1",
                    "remote_device": "switch-b",
                    "remote_port_id": 20,
                    "remote_device_id": 99,
                }
            ]
        }

        process_result = {
            "local_port": "Gi0/0",
            "remote_port": "Gi0/1",
            "remote_device": "switch-b",
            "remote_port_id": 20,
            "remote_device_id": 99,
            "remote_device_url": "/device/5/",
            "remote_port_url": "/interface/20/",
            "remote_port_name": "Gi0/1",
        }

        with (
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_object_or_404",
                return_value=mock_device,
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device",
                return_value=mock_device,
            ),
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="test-key"),
            patch.object(view, "process_remote_device", return_value=process_result),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.escape",
                side_effect=lambda x: x,
            ),
        ):
            mock_cache.get.return_value = cached_links
            response = view.post(mock_request)

        import json as json_mod2

        data = json_mod2.loads(response.content)
        assert data["status"] == "success"
        row = data["formatted_row"]
        # local_port is text (not a link) because interface was not found
        assert row["local_port"] == "Gi0/0"
        assert "cable_status" in row

    def test_cable_url_present_wraps_cable_status_in_anchor(self):
        """When cable_url is in link_data, cable_status is wrapped in an <a> tag (line 514)."""
        import json as json_mod

        view = self._make_view()

        mock_request = MagicMock()
        mock_request.body = json_mod.dumps(
            {
                "device_id": 1,
                "local_port_id": 10,
                "server_key": "default",
            }
        ).encode()

        mock_device = MagicMock()
        mock_device.virtual_chassis = None
        mock_device.id = 1

        mock_interface = MagicMock()
        mock_interface.pk = 99
        # librenms_id lookup returns the interface; name lookup not needed
        mock_device.interfaces.filter.return_value.first.side_effect = [mock_interface, None]

        cached_links = {
            "links": [
                {
                    "local_port_id": 10,
                    "local_port": "Gi0/0",
                    "remote_port": "Gi0/1",
                    "remote_device": "switch-b",
                    "remote_port_id": 20,
                    "remote_device_id": 99,
                }
            ]
        }

        process_result = {
            "local_port": "Gi0/0",
            "remote_port": "Gi0/1",
            "remote_device": "switch-b",
            "remote_port_id": 20,
            "remote_device_id": 99,
            "netbox_remote_device_id": 5,
            "remote_device_url": "/device/5/",
            "remote_port_url": "/interface/20/",
            "remote_port_name": "Gi0/1",
            "cable_status": "Cable Found",
            "cable_url": "/dcim/cables/42/",
            "can_create_cable": False,
        }

        with (
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_object_or_404",
                return_value=mock_device,
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device",
                return_value=mock_device,
            ),
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="test-key"),
            patch.object(view, "process_remote_device", return_value=process_result),
            patch.object(view, "check_cable_status", return_value=process_result),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.reverse",
                return_value="/dcim/interfaces/99/",
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.escape",
                side_effect=lambda x: x,
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_token",
                return_value="csrf-token",
            ),
        ):
            mock_cache.get.return_value = cached_links
            response = view.post(mock_request)

        import json as json_mod2

        data = json_mod2.loads(response.content)
        assert data["status"] == "success"
        # cable_url was present → cable_status should be wrapped in an anchor tag
        cable_status = data["formatted_row"]["cable_status"]
        assert '<a href="/dcim/cables/42/">' in cable_status


# =============================================================================
# TestIpAddressViewMethods  — get_object (line 27), get_ip_addresses (lines 31-32)
# =============================================================================


class TestIpAddressViewMethods:
    """Tests for BaseIPAddressTableView.get_object and get_ip_addresses."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        view = object.__new__(BaseIPAddressTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view.model = MagicMock()
        return view

    def test_get_object_calls_get_object_or_404(self):
        """get_object delegates to get_object_or_404 with the view's model."""
        view = self._make_view()
        mock_device = MagicMock()

        with patch(
            "netbox_librenms_plugin.views.base.ip_addresses_view.get_object_or_404",
            return_value=mock_device,
        ) as mock_get:
            result = view.get_object(42)

        mock_get.assert_called_once_with(view.model, pk=42)
        assert result is mock_device

    def test_get_ip_addresses_calls_api(self):
        """get_ip_addresses calls get_librenms_id then get_device_ips; stores librenms_id."""
        view = self._make_view()
        view._librenms_api.get_librenms_id.return_value = 99
        view._librenms_api.get_device_ips.return_value = (True, [{"port_id": 1}])

        obj = _mock_obj()
        result = view.get_ip_addresses(obj)

        view._librenms_api.get_librenms_id.assert_called_once_with(obj)
        view._librenms_api.get_device_ips.assert_called_once_with(99)
        assert result == (True, [{"port_id": 1}])
        assert view.librenms_id == 99


# =============================================================================
# TestEnrichIpDataPortInfo  — port_info truthy branch (lines 68-69)
# =============================================================================


class TestEnrichIpDataPortInfo:
    """Tests for enrich_ip_data when port_info is truthy → sets enriched_ip['interface_name']."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        view = object.__new__(BaseIPAddressTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_port_info_truthy_sets_interface_name(self):
        """When _get_port_info returns a dict, interface_name is set from it."""
        view = self._make_view()

        ip_data = [{"port_id": 1, "ip_address": "10.0.0.1", "prefix_length": 24}]
        obj = _mock_obj()
        obj.get_absolute_url.return_value = "/device/1/"

        port_info = {"ifName": "Gi0/0"}
        base_entry = {
            "ip_address": "10.0.0.1",
            "prefix_length": 24,
            "ip_with_mask": "10.0.0.1/24",
            "port_id": 1,
            "device": "test-device",
            "device_url": "/device/1/",
            "vrf_id": None,
            "vrfs": [],
        }
        prefetched = {
            "interfaces_by_librenms_id": {},
            "interfaces_by_name": {},
            "all_interfaces": [],
            "device": obj,
            "ip_addresses_map": {},
            "vrfs": [],
        }

        with (
            patch.object(view, "_prefetch_netbox_data", return_value=prefetched),
            patch.object(view, "_get_port_info", return_value=port_info),
            patch.object(view, "_create_base_ip_entry", return_value=dict(base_entry)),
            patch.object(view, "_add_interface_info_to_ip"),
        ):
            result = view.enrich_ip_data(ip_data, obj, "ifName")

        assert len(result) == 1
        assert result[0]["interface_name"] == "Gi0/0"

    def test_port_info_none_does_not_set_interface_name(self):
        """When _get_port_info returns None, interface_name is not set from it."""
        view = self._make_view()

        ip_data = [{"port_id": 1, "ip_address": "10.0.0.1", "prefix_length": 24}]
        obj = _mock_obj()
        obj.get_absolute_url.return_value = "/device/1/"

        base_entry = {
            "ip_address": "10.0.0.1",
            "prefix_length": 24,
            "ip_with_mask": "10.0.0.1/24",
            "port_id": 1,
            "device": "test-device",
            "device_url": "/device/1/",
            "vrf_id": None,
            "vrfs": [],
        }
        prefetched = {
            "interfaces_by_librenms_id": {},
            "interfaces_by_name": {},
            "all_interfaces": [],
            "device": obj,
            "ip_addresses_map": {},
            "vrfs": [],
        }

        with (
            patch.object(view, "_prefetch_netbox_data", return_value=prefetched),
            patch.object(view, "_get_port_info", return_value=None),
            patch.object(view, "_create_base_ip_entry", return_value=dict(base_entry)),
            patch.object(view, "_add_interface_info_to_ip"),
        ):
            result = view.enrich_ip_data(ip_data, obj, "ifName")

        assert len(result) == 1
        assert "interface_name" not in result[0]


# =============================================================================
# TestPreparContextInterfaceNameFieldNone  — line 237
# =============================================================================


class TestPrepareContextInterfaceNameFieldNone:
    """Tests for _prepare_context when interface_name_field is None (line 237)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        view = object.__new__(BaseIPAddressTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_calls_get_interface_name_field_when_none(self):
        """When interface_name_field=None, _prepare_context calls get_interface_name_field."""
        view = self._make_view()

        obj = _mock_obj()
        request = _mock_request()

        with (
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="test-key"),
            patch(
                "netbox_librenms_plugin.views.base.ip_addresses_view.get_interface_name_field",
                return_value="ifName",
            ) as mock_gif,
        ):
            mock_cache.get.return_value = None  # no cached data → returns None early
            result = view._prepare_context(request, obj, None, fetch_fresh=False)

        mock_gif.assert_called_once_with(request)
        assert result is None  # returns None because cache miss


# =============================================================================
# TestSingleIPAddressVerifyViewGetObject  — _get_object (lines 325-339)
# =============================================================================


class TestSingleIPAddressVerifyViewGetObject:
    """Tests for SingleIPAddressVerifyView._get_object."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

        view = object.__new__(SingleIPAddressVerifyView)
        return view

    def test_device_type_calls_get_object_or_404_for_device(self):
        """object_type='device' → get_object_or_404(Device, pk=object_id)."""
        from dcim.models import Device

        view = self._make_view()
        mock_device = MagicMock()

        with patch(
            "netbox_librenms_plugin.views.base.ip_addresses_view.get_object_or_404",
            return_value=mock_device,
        ) as mock_get:
            result = view._get_object(1, "device")

        mock_get.assert_called_once_with(Device, pk=1)
        assert result is mock_device

    def test_vm_type_calls_get_object_or_404_for_vm(self):
        """object_type='virtualmachine' → get_object_or_404(VirtualMachine, pk=object_id)."""
        from virtualization.models import VirtualMachine

        view = self._make_view()
        mock_vm = MagicMock()

        with patch(
            "netbox_librenms_plugin.views.base.ip_addresses_view.get_object_or_404",
            return_value=mock_vm,
        ) as mock_get:
            result = view._get_object(2, "virtualmachine")

        mock_get.assert_called_once_with(VirtualMachine, pk=2)
        assert result is mock_vm

    def test_no_type_finds_device(self):
        """No type given → tries Device.objects.filter; returns device when found."""
        view = self._make_view()

        mock_device = MagicMock()

        with patch("netbox_librenms_plugin.views.base.ip_addresses_view.Device") as MockDevice:
            MockDevice.objects.filter.return_value.first.return_value = mock_device
            result = view._get_object(1, None)

        assert result is mock_device

    def test_no_type_device_not_found_tries_vm(self):
        """No type, Device not found → tries VirtualMachine; returns VM when found."""
        view = self._make_view()

        mock_vm = MagicMock()

        with (
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.Device") as MockDevice,
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.VirtualMachine") as MockVM,
        ):
            MockDevice.objects.filter.return_value.first.return_value = None
            MockVM.objects.filter.return_value.first.return_value = mock_vm
            result = view._get_object(2, None)

        assert result is mock_vm

    def test_no_type_neither_found_raises_http404(self):
        """No type, nothing found → raises Http404."""
        from django.http import Http404

        view = self._make_view()

        with (
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.Device") as MockDevice,
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.VirtualMachine") as MockVM,
        ):
            MockDevice.objects.filter.return_value.first.return_value = None
            MockVM.objects.filter.return_value.first.return_value = None

            try:
                view._get_object(99, None)
                assert False, "Expected Http404"
            except Http404:
                pass


# =============================================================================
# TestSingleIPAddressVerifyViewParseIp  — _parse_ip_address (lines 346-356)
# =============================================================================


class TestSingleIPAddressVerifyViewParseIp:
    """Tests for SingleIPAddressVerifyView._parse_ip_address."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

        return object.__new__(SingleIPAddressVerifyView)

    def test_valid_ipv4_with_prefix(self):
        """'192.168.1.1/24' → ('192.168.1.1', 24)."""
        view = self._make_view()
        addr, prefix = view._parse_ip_address("192.168.1.1/24")
        assert addr == "192.168.1.1"
        assert prefix == 24

    def test_valid_ipv6_with_prefix(self):
        """'2001:db8::1/64' → ('2001:db8::1', 64)."""
        view = self._make_view()
        addr, prefix = view._parse_ip_address("2001:db8::1/64")
        assert addr == "2001:db8::1"
        assert prefix == 64

    def test_invalid_prefix_raises_value_error(self):
        """'192.168.1.1/abc' → ValueError with 'Invalid prefix length'."""
        view = self._make_view()
        try:
            view._parse_ip_address("192.168.1.1/abc")
            assert False, "Expected ValueError"
        except ValueError as exc:
            assert "Invalid prefix length" in str(exc)

    def test_missing_prefix_raises_value_error(self):
        """'192.168.1.1' (no slash) → ValueError with 'Prefix length is missing'."""
        view = self._make_view()
        try:
            view._parse_ip_address("192.168.1.1")
            assert False, "Expected ValueError"
        except ValueError as exc:
            assert "Prefix length is missing" in str(exc)


# =============================================================================
# TestSingleIPAddressVerifyViewFindInCache  — _find_in_cache (lines 360-367)
# =============================================================================


class TestSingleIPAddressVerifyViewFindInCache:
    """Tests for SingleIPAddressVerifyView._find_in_cache."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

        return object.__new__(SingleIPAddressVerifyView)

    def test_no_cached_data_returns_triple_none(self):
        """cached_data=None → (None, None, None)."""
        view = self._make_view()
        result = view._find_in_cache(None, "192.168.1.1", 24)
        assert result == (None, None, None)

    def test_empty_cache_returns_triple_none(self):
        """cached_data with no ip_addresses → (None, None, None)."""
        view = self._make_view()
        result = view._find_in_cache({"ip_addresses": []}, "192.168.1.1", 24)
        assert result == (None, None, None)

    def test_match_returns_entry_vrf_id_port_id(self):
        """Matching entry → (entry, vrf_id, port_id)."""
        view = self._make_view()
        entry = {"ip_address": "192.168.1.1", "prefix_length": 24, "vrf_id": 5, "port_id": 10}
        cached = {"ip_addresses": [entry]}
        ip_entry, vrf_id, port_id = view._find_in_cache(cached, "192.168.1.1", 24)
        assert ip_entry is entry
        assert vrf_id == 5
        assert port_id == 10

    def test_no_match_returns_triple_none(self):
        """Entries present but no match → (None, None, None)."""
        view = self._make_view()
        entry = {"ip_address": "10.0.0.1", "prefix_length": 16, "vrf_id": None, "port_id": 1}
        cached = {"ip_addresses": [entry]}
        result = view._find_in_cache(cached, "192.168.1.1", 24)
        assert result == (None, None, None)


# =============================================================================
# TestSingleIPAddressVerifyViewFindExistingIp  — _find_existing_ip (lines 373-387)
# =============================================================================


class TestSingleIPAddressVerifyViewFindExistingIp:
    """Tests for SingleIPAddressVerifyView._find_existing_ip."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

        return object.__new__(SingleIPAddressVerifyView)

    def test_ip_not_found_returns_false_false_none(self):
        """IP not in NetBox → (False, False, None)."""
        view = self._make_view()

        with patch("netbox_librenms_plugin.views.base.ip_addresses_view.IPAddress") as MockIP:
            MockIP.objects.filter.return_value.first.return_value = None
            result = view._find_existing_ip("192.168.1.1", 24, vrf_id=None)

        assert result == (False, False, None)

    def test_ip_found_with_vrf_id_checks_specific_vrf(self):
        """IP exists; vrf_id given → queries for specific VRF membership."""
        view = self._make_view()

        mock_ip = MagicMock()
        mock_ip.get_absolute_url.return_value = "/ip/1/"

        with patch("netbox_librenms_plugin.views.base.ip_addresses_view.IPAddress") as MockIP:
            MockIP.objects.filter.return_value.first.return_value = mock_ip
            MockIP.objects.filter.return_value.exists.return_value = True
            exists_any, exists_vrf, url = view._find_existing_ip("192.168.1.1", 24, vrf_id=5)

        assert exists_any is True
        assert exists_vrf is True
        assert url == "/ip/1/"
        # Verify VRF-scoped second query was made
        MockIP.objects.filter.assert_any_call(address="192.168.1.1/24", vrf__id=5)

    def test_ip_found_without_vrf_id_checks_global(self):
        """IP exists; vrf_id=None → queries for global VRF (vrf__isnull=True)."""
        view = self._make_view()

        mock_ip = MagicMock()
        mock_ip.get_absolute_url.return_value = "/ip/2/"

        with patch("netbox_librenms_plugin.views.base.ip_addresses_view.IPAddress") as MockIP:
            MockIP.objects.filter.return_value.first.return_value = mock_ip
            MockIP.objects.filter.return_value.exists.return_value = True
            exists_any, exists_vrf, url = view._find_existing_ip("10.0.0.1", 8, vrf_id=None)

        assert exists_any is True
        assert exists_vrf is True
        assert url == "/ip/2/"
        # Verify global VRF second query was made
        MockIP.objects.filter.assert_any_call(address="10.0.0.1/8", vrf__isnull=True)


# =============================================================================
# TestSingleIPAddressVerifyViewDetermineStatus  — _determine_status (lines 393-404)
# =============================================================================


class TestSingleIPAddressVerifyViewDetermineStatus:
    """Tests for SingleIPAddressVerifyView._determine_status."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

        return object.__new__(SingleIPAddressVerifyView)

    def test_exists_and_in_specific_vrf_returns_matched(self):
        """IP exists AND is in the specified VRF → 'matched'."""
        view = self._make_view()
        result = view._determine_status(True, True, None, 5)
        assert result == "matched"

    def test_exists_not_in_specific_vrf_returns_update(self):
        """IP exists but NOT in the specified VRF → 'update'."""
        view = self._make_view()
        result = view._determine_status(True, False, None, 5)
        assert result == "update"

    def test_not_exists_restoring_original_vrf_returns_matched(self):
        """IP doesn't exist; original_vrf_id == vrf_id → 'matched' (restoring original)."""
        view = self._make_view()
        result = view._determine_status(False, False, 5, 5)
        assert result == "matched"

    def test_not_exists_different_vrf_returns_sync(self):
        """IP doesn't exist; vrf_id differs from original → 'sync'."""
        view = self._make_view()
        result = view._determine_status(False, False, 3, 5)
        assert result == "sync"

    def test_not_exists_no_original_vrf_returns_sync(self):
        """IP doesn't exist; original_vrf_id=None → 'sync'."""
        view = self._make_view()
        result = view._determine_status(False, False, None, None)
        assert result == "sync"


# =============================================================================
# TestSingleIPAddressVerifyViewPost  — post() method (lines 410-495)
# =============================================================================


class TestSingleIPAddressVerifyViewPost:
    """Tests for SingleIPAddressVerifyView.post()."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

        view = object.__new__(SingleIPAddressVerifyView)
        # CacheMixin needs server_key attr indirectly via get_cache_key
        view._librenms_api = MagicMock()
        return view

    def test_no_ip_address_returns_400(self):
        """Missing ip_address → JsonResponse 400."""
        import json as json_mod

        view = self._make_view()
        req = MagicMock()
        req.body = json_mod.dumps({"device_id": 1}).encode()

        response = view.post(req)
        assert response.status_code == 400
        data = json_mod.loads(response.content)
        assert data["status"] == "error"

    def test_no_object_id_returns_400(self):
        """Missing device_id → JsonResponse 400."""
        import json as json_mod

        view = self._make_view()
        req = MagicMock()
        req.body = json_mod.dumps({"ip_address": "10.0.0.1/24"}).encode()

        response = view.post(req)
        assert response.status_code == 400

    def test_http404_on_get_object_returns_404(self):
        """When _get_object raises Http404 → JsonResponse 404."""
        import json as json_mod
        from django.http import Http404

        view = self._make_view()
        req = MagicMock()
        req.body = json_mod.dumps({"ip_address": "10.0.0.1/24", "device_id": 999}).encode()

        with patch.object(view, "_get_object", side_effect=Http404("not found")):
            response = view.post(req)

        assert response.status_code == 404

    def test_invalid_ip_parse_returns_400(self):
        """ValueError from _parse_ip_address → JsonResponse 400."""
        import json as json_mod

        view = self._make_view()
        req = MagicMock()
        req.body = json_mod.dumps({"ip_address": "bad-ip", "device_id": 1}).encode()

        mock_obj = MagicMock()

        with patch.object(view, "_get_object", return_value=mock_obj):
            with patch.object(view, "_parse_ip_address", side_effect=ValueError("Prefix length is missing")):
                response = view.post(req)

        assert response.status_code == 400

    def test_success_returns_formatted_row(self):
        """Valid request → JsonResponse 200 with status, ip_address, formatted_row."""
        import json as json_mod

        view = self._make_view()
        req = MagicMock()
        req.body = json_mod.dumps(
            {
                "ip_address": "10.0.0.1/24",
                "device_id": 1,
                "vrf_id": None,
                "server_key": "default",
            }
        ).encode()

        mock_obj = MagicMock()
        mock_obj.name = "device1"
        mock_obj.get_absolute_url.return_value = "/device/1/"
        mock_obj.interfaces.first.return_value = None

        with (
            patch.object(view, "_get_object", return_value=mock_obj),
            patch.object(view, "_parse_ip_address", return_value=("10.0.0.1", 24)),
            patch.object(view, "get_cache_key", return_value="cache-key"),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.cache") as mock_cache,
            patch.object(view, "_find_in_cache", return_value=(None, None, None)),
            patch.object(view, "_find_existing_ip", return_value=(False, False, None)),
            patch.object(view, "_determine_status", return_value="sync"),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.IPAddressTable") as MockTable,
        ):
            mock_cache.get.return_value = None
            mock_table_instance = MagicMock()
            mock_table_instance.render_status.return_value = "<span>sync</span>"
            MockTable.return_value = mock_table_instance

            response = view.post(req)

        assert response.status_code == 200
        data = json_mod.loads(response.content)
        assert data["status"] == "success"
        assert data["ip_address"] == "10.0.0.1/24"
        assert "formatted_row" in data

    def test_success_with_cache_entry_updates_record(self):
        """When cache has an entry for the IP, updated_record is enriched with it."""
        import json as json_mod

        view = self._make_view()
        req = MagicMock()
        req.body = json_mod.dumps(
            {
                "ip_address": "10.0.0.1/24",
                "device_id": 1,
                "vrf_id": None,
                "server_key": "default",
            }
        ).encode()

        mock_obj = MagicMock()
        mock_obj.name = "device1"
        mock_obj.get_absolute_url.return_value = "/device/1/"

        cache_entry = {
            "ip_address": "10.0.0.1",
            "prefix_length": 24,
            "interface_name": "eth0",
            "interface_url": "/interface/1/",
            "vrf_id": 5,
            "status": "update",
        }

        with (
            patch.object(view, "_get_object", return_value=mock_obj),
            patch.object(view, "_parse_ip_address", return_value=("10.0.0.1", 24)),
            patch.object(view, "get_cache_key", return_value="cache-key"),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.cache") as mock_cache,
            patch.object(
                view,
                "_find_in_cache",
                return_value=(cache_entry, 5, 10),
            ),
            patch.object(view, "_find_existing_ip", return_value=(True, True, "/ip/1/")),
            patch.object(view, "_determine_status", return_value="matched"),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.IPAddressTable") as MockTable,
        ):
            mock_cache.get.return_value = {"ip_addresses": [cache_entry]}
            mock_table_instance = MagicMock()
            mock_table_instance.render_status.return_value = "<span>matched</span>"
            MockTable.return_value = mock_table_instance

            response = view.post(req)

        assert response.status_code == 200
        data = json_mod.loads(response.content)
        assert data["status"] == "success"
        # Verify cache entry fields (interface_name, interface_url) were merged into
        # the updated_record that is passed to render_status
        assert mock_table_instance.render_status.call_count == 1
        rendered_record = mock_table_instance.render_status.call_args[0][1]
        assert rendered_record["interface_name"] == "eth0"
        assert rendered_record["interface_url"] == "/interface/1/"

    def test_invalid_json_returns_400(self):
        """Malformed JSON body → JsonResponse 400."""
        import json as json_mod

        view = self._make_view()
        req = MagicMock()
        req.body = b"not-json"  # will cause json.loads to fail

        response = view.post(req)
        assert response.status_code == 400
        data = json_mod.loads(response.content)
        assert data["status"] == "error"

    def test_interface_from_device_used_when_no_cache(self):
        """When no cache entry (port_id=None), first device interface is used."""
        import json as json_mod

        view = self._make_view()
        req = MagicMock()
        req.body = json_mod.dumps(
            {
                "ip_address": "10.0.0.1/24",
                "device_id": 1,
                "vrf_id": None,
                "server_key": "default",
            }
        ).encode()

        mock_obj = MagicMock()
        mock_obj.name = "device1"
        mock_obj.get_absolute_url.return_value = "/device/1/"

        mock_iface = MagicMock()
        mock_iface.name = "eth0"
        mock_iface.get_absolute_url.return_value = "/interface/1/"
        mock_obj.interfaces.first.return_value = mock_iface

        with (
            patch.object(view, "_get_object", return_value=mock_obj),
            patch.object(view, "_parse_ip_address", return_value=("10.0.0.1", 24)),
            patch.object(view, "get_cache_key", return_value="cache-key"),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.cache") as mock_cache,
            patch.object(view, "_find_existing_ip", return_value=(False, False, None)),
            patch.object(view, "_determine_status", return_value="sync"),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.IPAddressTable") as MockTable,
        ):
            # Cache has an entry for this IP but no port_id → interfaces.first() fallback runs
            mock_cache.get.return_value = {"ip_addresses": [{"ip_address": "10.0.0.1", "prefix_length": 24}]}
            mock_table_instance = MagicMock()
            mock_table_instance.render_status.return_value = "<span>sync</span>"
            MockTable.return_value = mock_table_instance

            response = view.post(req)

        assert response.status_code == 200
        # Cache entry found but has no port_id → first device interface used
        mock_obj.interfaces.first.assert_called_once()

    def test_verify_with_non_default_server_key(self):
        """server_key='secondary' propagates to get_cache_key call."""
        import json as json_mod

        view = self._make_view()
        req = MagicMock()
        req.body = json_mod.dumps(
            {
                "ip_address": "192.168.1.1/24",
                "device_id": 2,
                "vrf_id": None,
                "server_key": "secondary",
            }
        ).encode()

        mock_obj = MagicMock()
        mock_obj.name = "device2"
        mock_obj.get_absolute_url.return_value = "/device/2/"
        mock_obj.interfaces.first.return_value = None

        with (
            patch.object(view, "_get_object", return_value=mock_obj),
            patch.object(view, "_parse_ip_address", return_value=("192.168.1.1", 24)),
            patch.object(view, "get_cache_key", return_value="secondary-cache-key") as mock_get_cache_key,
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.cache") as mock_cache,
            patch.object(view, "_find_in_cache", return_value=(None, None, None)),
            patch.object(view, "_find_existing_ip", return_value=(False, False, None)),
            patch.object(view, "_determine_status", return_value="sync"),
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.IPAddressTable") as MockTable,
        ):
            mock_cache.get.return_value = None
            mock_table_instance = MagicMock()
            mock_table_instance.render_status.return_value = "<span>sync</span>"
            MockTable.return_value = mock_table_instance

            response = view.post(req)

        assert response.status_code == 200
        mock_get_cache_key.assert_called_once_with(mock_obj, "ip_addresses", "secondary")
        mock_cache.get.assert_called_once_with("secondary-cache-key")


# =============================================================================
# TestGetDeviceByIdOrNameLine124  — librenms_id DoesNotExist fallthrough (line 124)
# =============================================================================


class TestGetDeviceByIdOrNameLine124:
    """Test that DoesNotExist on librenms_id lookup falls through to name lookup (line 124)."""

    def test_librenms_id_doesnotexist_falls_through_to_name(self):
        """remote_device_id provided but DoesNotExist → falls through to name match."""
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"

        mock_device = MagicMock()

        with patch("netbox_librenms_plugin.views.base.cables_view.Device") as MockDevice:

            class _DoesNotExist(Exception):
                pass

            MockDevice.DoesNotExist = _DoesNotExist
            # First call: librenms_id lookup → DoesNotExist (line 124: pass)
            # Second call: name lookup → success
            MockDevice.objects.get.side_effect = [_DoesNotExist, mock_device]

            device, found, error = view.get_device_by_id_or_name(42, "switch-a")

        assert found is True
        assert device is mock_device


# =============================================================================
# TestGetDeviceByIdOrNameSimpleHostnameMultiple  — lines 144-145
# =============================================================================


class TestGetDeviceByIdOrNameSimpleHostnameMultiple:
    """MultipleObjectsReturned when searching by simple hostname (lines 144-145)."""

    def test_simple_hostname_multiple_returns_error(self):
        """FQDN DoesNotExist, simple hostname raises MultipleObjectsReturned → (None, False, msg)."""
        from django.core.exceptions import MultipleObjectsReturned
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"

        with patch("netbox_librenms_plugin.views.base.cables_view.Device") as MockDevice:

            class _DoesNotExist(Exception):
                pass

            MockDevice.DoesNotExist = _DoesNotExist
            # remote_device_id=None → skip librenms_id
            # First get() (FQDN) → DoesNotExist
            # Second get() (simple hostname) → MultipleObjectsReturned
            MockDevice.objects.get.side_effect = [_DoesNotExist, MultipleObjectsReturned]

            device, found, error = view.get_device_by_id_or_name(None, "switch.example.com")

        assert device is None
        assert found is False
        assert error is not None
        assert "switch.example.com" in error


# =============================================================================
# TestEnrichLocalPortVCNameFallback  — line 174 (VC name fallback)
# =============================================================================


class TestEnrichLocalPortVCNameFallback:
    """Tests for enrich_local_port VC path name fallback when librenms_id miss (line 174)."""

    def test_vc_name_fallback_when_librenms_id_miss(self):
        """VC path: librenms_id lookup returns None → falls back to name lookup (line 174)."""
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"

        obj = MagicMock()
        obj.virtual_chassis = MagicMock()  # truthy

        mock_interface = MagicMock()
        mock_interface.pk = 88

        mock_member = MagicMock()
        # librenms_id lookup (first .first()) returns None → triggers name fallback
        # name lookup (second .first()) returns the interface
        mock_member.interfaces.filter.return_value.first.side_effect = [None, mock_interface]

        link = {"local_port": "Gi0/0", "local_port_id": 10}

        with (
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_virtual_chassis_member",
                return_value=mock_member,
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.reverse",
                return_value="/dcim/interfaces/88/",
            ),
        ):
            view.enrich_local_port(link, obj)

        assert link.get("netbox_local_interface_id") == 88

    def test_vc_no_local_port_id_goes_straight_to_name(self):
        """VC path with local_port_id=None → skips librenms_id, goes to name lookup (line 174)."""
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"

        obj = MagicMock()
        obj.virtual_chassis = MagicMock()  # truthy

        mock_interface = MagicMock()
        mock_interface.pk = 77

        mock_member = MagicMock()
        mock_member.interfaces.filter.return_value.first.return_value = mock_interface

        # local_port_id=None → `if local_port_id:` is False → skips librenms_id
        # → goes directly to line 174 (name lookup)
        link = {"local_port": "Gi0/0", "local_port_id": None}

        with (
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_virtual_chassis_member",
                return_value=mock_member,
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.reverse",
                return_value="/dcim/interfaces/77/",
            ),
        ):
            view.enrich_local_port(link, obj)

        assert link.get("netbox_local_interface_id") == 77


# =============================================================================
# TestPostHandlerCanCreateCable  — lines 519-525 (can_create_cable form)
# =============================================================================


class TestPostHandlerCanCreateCable:
    """Tests for SingleCableVerifyView.post() can_create_cable branch (lines 519-525)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        view = object.__new__(SingleCableVerifyView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_can_create_cable_adds_form_action(self):
        """can_create_cable=True → formatted_row['actions'] contains form."""
        import json as json_mod

        view = self._make_view()

        mock_request = MagicMock()
        mock_request.body = json_mod.dumps(
            {
                "device_id": 1,
                "local_port_id": 10,
                "server_key": "default",
            }
        ).encode()

        mock_device = MagicMock()
        mock_device.virtual_chassis = None
        mock_device.id = 1

        mock_interface = MagicMock()
        mock_interface.pk = 99
        mock_device.interfaces.filter.return_value.first.side_effect = [mock_interface, None]

        cached_links = {
            "links": [
                {
                    "local_port_id": 10,
                    "local_port": "Gi0/0",
                    "remote_port": "Gi0/1",
                    "remote_device": "switch-b",
                    "remote_port_id": 20,
                    "remote_device_id": 99,
                }
            ]
        }

        process_result = {
            "local_port": "Gi0/0",
            "remote_port": "Gi0/1",
            "remote_device": "switch-b",
            "remote_port_id": 20,
            "remote_device_id": 99,
            "netbox_remote_device_id": 5,
            "remote_device_url": "/device/5/",
            "remote_port_url": "/interface/20/",
            "remote_port_name": "Gi0/1",
            "cable_status": "No Cable",
            "can_create_cable": True,  # triggers lines 519-525
        }

        with (
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_object_or_404",
                return_value=mock_device,
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device",
                return_value=mock_device,
            ),
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="test-key"),
            patch.object(view, "process_remote_device", return_value=process_result),
            patch.object(view, "check_cable_status", return_value=process_result),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.reverse",
                side_effect=[
                    "/dcim/interfaces/99/",
                    "/plugins/librenms/sync/cables/1/",
                ],
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.escape",
                side_effect=lambda x: x,
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_token",
                return_value="csrf-token",
            ),
        ):
            mock_cache.get.return_value = cached_links
            response = view.post(mock_request)

        import json as json_mod2

        data = json_mod2.loads(response.content)
        assert data["status"] == "success"
        # can_create_cable=True → actions should contain a form
        assert "form" in data["formatted_row"]["actions"]
        assert "Sync Cable" in data["formatted_row"]["actions"]


# =============================================================================
# TestPostHandlerInterfaceNotFoundBranches  — lines 554, 559
# =============================================================================


class TestPostHandlerInterfaceNotFoundBranches:
    """Tests for the cable_status branches in interface-not-found path (lines 554, 559)."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        view = object.__new__(SingleCableVerifyView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def _run_post(self, view, process_result):
        """Helper: run the post with a given process_result dict."""
        import json as json_mod

        mock_request = MagicMock()
        mock_request.body = json_mod.dumps(
            {
                "device_id": 1,
                "local_port_id": 10,
                "server_key": "default",
            }
        ).encode()

        mock_device = MagicMock()
        mock_device.virtual_chassis = None
        mock_device.id = 1
        mock_device.interfaces.filter.return_value.first.return_value = None  # no interface

        cached_links = {
            "links": [
                {
                    "local_port_id": 10,
                    "local_port": "Gi0/0",
                    "remote_port": "Gi0/1",
                    "remote_device": "switch-b",
                    "remote_port_id": 20,
                    "remote_device_id": 99,
                }
            ]
        }

        with (
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_object_or_404",
                return_value=mock_device,
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device",
                return_value=mock_device,
            ),
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="test-key"),
            patch.object(view, "process_remote_device", return_value=process_result),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.escape",
                side_effect=lambda x: x,
            ),
        ):
            mock_cache.get.return_value = cached_links
            response = view.post(mock_request)

        import json as json_mod2

        return json_mod2.loads(response.content)

    def test_no_remote_device_url_sets_device_not_found(self):
        """remote_device present, no remote_device_url → 'Device Not Found in NetBox' (line 554)."""
        view = self._make_view()

        process_result = {
            "local_port": "Gi0/0",
            "remote_port": "Gi0/1",
            "remote_device": "switch-b",  # truthy remote_device_name
            "remote_port_id": 20,
            # No remote_device_url → triggers line 554
            "remote_port_name": "Gi0/1",
        }

        data = self._run_post(view, process_result)
        assert data["status"] == "success"
        assert data["formatted_row"]["cable_status"] == "Device Not Found in NetBox"

    def test_device_url_but_no_port_url_sets_missing_interface(self):
        """remote_device_url present, no remote_port_url → 'Missing Interface' (line 559)."""
        view = self._make_view()

        process_result = {
            "local_port": "Gi0/0",
            "remote_port": "Gi0/1",
            "remote_device": "switch-b",
            "remote_port_id": 20,
            "remote_device_url": "/device/5/",  # device found
            # No remote_port_url → elif condition False → else line 559
            "remote_port_name": "Gi0/1",
        }

        data = self._run_post(view, process_result)
        assert data["status"] == "success"
        assert data["formatted_row"]["cable_status"] == "Missing Interface"
