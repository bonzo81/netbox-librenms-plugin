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

import pytest

from netbox_librenms_plugin.tests.conftest import make_device, make_interface


def _seed_lib_id(iface, value, server_key="default"):
    """Seed an interface's librenms_id custom field under *server_key*."""
    iface.custom_field_data["librenms_id"] = {server_key: value}
    iface.save()


def _vc_with_member(vc_name, master_name, member_name, member_pos=1):
    """Create a real VirtualChassis with a master (pos 9) and one member at *member_pos*."""
    from dcim.models import VirtualChassis

    vc = VirtualChassis.objects.create(name=vc_name)
    master = make_device(master_name)
    master.virtual_chassis = vc
    master.vc_position = 9
    master.save()
    member = make_device(member_name)
    member.virtual_chassis = vc
    member.vc_position = member_pos
    member.save()
    return master, member


# =============================================================================
# Helpers
# =============================================================================


def _q_leaves(q):
    """Flatten a Django Q into a set of (lookup, value) leaf tuples."""
    from django.db.models import Q

    leaves = set()
    for child in q.children:
        if isinstance(child, Q):
            leaves |= _q_leaves(child)
        else:
            leaves.add(child)
    return leaves


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


def _authorized_superuser(tag):
    """A real superuser so the object-perm gate passes and restrict() resolves the real device."""
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create_superuser(username=f"cbv2-{tag}", email="", password="x")


def _real_cable_device(tag, *, vc=False, bound_port_id=None, iface_name="Gi0/0"):
    """A real Device for cable-verify post() tests; optionally in a VC and/or with a librenms-id-bound interface."""
    from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site, VirtualChassis

    mfr, _ = Manufacturer.objects.get_or_create(name=f"Cbv2Mfr-{tag}", slug=f"cbv2mfr-{tag}")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model=f"Cbv2DT-{tag}", slug=f"cbv2dt-{tag}")
    role, _ = DeviceRole.objects.get_or_create(name="Cbv2Role", slug="cbv2role")
    site, _ = Site.objects.get_or_create(name="Cbv2Site", slug="cbv2site")
    extra = {}
    if vc:
        extra["virtual_chassis"] = VirtualChassis.objects.create(name=f"Cbv2VC-{tag}")
        extra["vc_position"] = 1
    device = Device.objects.create(name=f"cbv2-{tag}", device_type=dt, role=role, site=site, status="active", **extra)
    if bound_port_id is not None:
        iface = Interface.objects.create(device=device, name=iface_name, type="1000base-t")
        iface.custom_field_data = {"librenms_id": {"default": bound_port_id}}
        iface.save()
    return device


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

    def test_float_returns_match_nothing_q(self):
        """A float (e.g. 42.7) must match nothing, not be truncated by int() to bind device 42 — mirrors coerce_librenms_id's int/str-only contract."""
        from netbox_librenms_plugin.views.base.cables_view import _librenms_id_q
        from django.db.models import Q

        result = _librenms_id_q("default", 42.7)
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

    def test_dict_form_paths_included(self):
        """Dict-form devices ({server_key: {"id": N, "oob": {...}}}) must resolve too: the Q must query the __id and __oob__id JSON paths, not just the scalar path."""
        from netbox_librenms_plugin.views.base.cables_view import _librenms_id_q

        leaves = _q_leaves(_librenms_id_q("default", 42))
        # Both dict JSON paths must be queried, with BOTH the int and the JSON-string
        # variant (JSON may store the id as "42"). Assert on the composed predicates rather
        # than the Q's string form so framework formatting changes don't break the test.
        assert ("custom_field_data__librenms_id__default__id", 42) in leaves
        assert ("custom_field_data__librenms_id__default__id", "42") in leaves
        assert ("custom_field_data__librenms_id__default__oob__id", 42) in leaves
        assert ("custom_field_data__librenms_id__default__oob__id", "42") in leaves

    def test_include_oob_false_drops_oob_path(self):
        """Device resolution must exclude the OOB-controller path: a device's own LibreNMS id is not a reference to its OOB controller's id, so include_oob=False keeps the host/id/legacy paths but no __oob__id leaf."""
        from netbox_librenms_plugin.views.base.cables_view import _librenms_id_q

        leaves = _q_leaves(_librenms_id_q("default", 42, include_oob=False))
        assert ("custom_field_data__librenms_id__default__id", 42) in leaves
        assert ("custom_field_data__librenms_id__default", 42) in leaves
        assert all("oob" not in str(path) for path, _ in leaves)

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

    def test_float_value_returns_match_nothing_q(self):
        """Issue #103: a float like 1.9 must NOT int()-truncate to 1 and match device id 1."""
        from django.db.models import Q
        from netbox_librenms_plugin.views.base.cables_view import _librenms_id_q

        result = _librenms_id_q("default", 1.9)
        expected = Q(pk__isnull=True) & Q(pk__isnull=False)
        assert str(result) == str(expected)

    def test_non_positive_int_returns_match_nothing_q(self):
        """Zero / negative values can't be a valid librenms_id, so they match nothing."""
        from django.db.models import Q
        from netbox_librenms_plugin.views.base.cables_view import _librenms_id_q

        expected = str(Q(pk__isnull=True) & Q(pk__isnull=False))
        assert str(_librenms_id_q("default", 0)) == expected
        assert str(_librenms_id_q("default", -5)) == expected


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
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        allowed = make_device("getobj-allowed-cable")
        hidden = make_device("getobj-hidden-cable")
        user = make_user_with_perms("getobj-viewer-cable", [("view", Device)], constraints={"name": allowed.name})
        view = make_view(BaseCableTableView, make_request("get", user=user))
        view.model = Device

        assert view.get_object(allowed.pk).pk == allowed.pk
        with pytest.raises(Http404):
            view.get_object(hidden.pk)

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

    def test_oob_only_no_host_id_returns_empty_before_cache(self):
        """An OOB-only device (librenms_id None) must return empty ports BEFORE consulting the cache, so a stale host-ports snapshot from a prior mapped refresh can't resurface into the new render."""
        view = self._make_view()
        view.librenms_id = None
        obj = _mock_obj()

        with patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache:
            mock_cache.get.return_value = {"ports": [{"port_id": 7, "ifName": "STALE-HOST-PORT"}]}
            with patch.object(view, "get_cache_key", return_value="test-key"):
                result = view.get_ports_data(obj)

        assert result == {"ports": []}  # the stale cached host ports must NOT be served
        mock_cache.get.assert_not_called()  # the cache is not even consulted for a hostless device
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


class TestGetLinksDataOobOnlyEmptyRefresh:
    """OOB-only mapping (no host librenms_id) with a valid empty OOB result must return [] — not None — so _prepare_context() can overwrite the cache with the empty snapshot rather than skip it and leave stale OOB rows behind after a genuine empty refresh."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view.request = _mock_request()
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_oob_only_valid_empty_returns_empty_list_not_none(self):
        view = self._make_view()
        obj = _mock_obj()
        obj.consoleserverports.exists.return_value = False  # no serial CSP rows to append

        # OOB-only: no host librenms_id, so the host get_device_links() call fails and records
        # _links_fetch_error even though no host fetch was meaningfully attempted; the OOB
        # controller (id 99) validly returns no links.
        view._librenms_api.get_librenms_id.return_value = None

        def _links(dev_id):
            if dev_id is None:  # host fetch — there is no host mapping
                return (False, {"error": "Device not found in LibreNMS"})
            return (True, {"links": []})  # OOB controller: valid, empty

        view._librenms_api.get_device_links.side_effect = _links
        view._librenms_api.get_ports.return_value = (True, {"ports": []})

        with (
            patch.object(view, "get_ports_data", return_value={"ports": []}),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_interface_name_field",
                return_value="ifName",
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device",
                return_value=obj,
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_oob",
                return_value={"id": 99},
            ),
        ):
            result = view.get_links_data(obj)

        # A successful empty OOB-only refresh yields [] (cacheable), not None (a mislabeled
        # "fetch failure" that _prepare_context would refuse to cache).
        assert result == []
        # The host-fetch error WAS recorded (no host mapping) — proving the guard's None branch
        # would have fired without the OOB-scoped exception.
        assert view._links_fetch_error is not None
        assert view.librenms_id is None

    def test_oob_only_skips_wasteful_host_link_and_port_calls(self):
        """An OOB-only device (no host librenms_id) must not issue host get_device_links(None)/get_ports(None) — those GET /devices/None/... and always 404; only the OOB controller id is fetched, and the OOB rows still render."""
        view = self._make_view()
        obj = _mock_obj()
        obj.consoleserverports.exists.return_value = False
        view._librenms_api.get_librenms_id.return_value = None
        # Only the OOB controller (99) should ever reach the link/port fetches.
        view._librenms_api.get_device_links.return_value = (True, {"links": []})
        view._librenms_api.get_ports.return_value = (True, {"ports": []})

        with (
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="k"),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_interface_name_field",
                return_value="ifName",
            ),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=obj),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_oob", return_value={"id": 99}),
        ):
            mock_cache.get.return_value = None  # force get_ports_data past its cache short-circuit
            result = view.get_links_data(obj)

        # The host fetches are skipped entirely: get_device_links / get_ports are called exactly
        # once each, for the OOB controller (99) — never with the None host id (which would GET
        # /devices/None/...). assert_called_once_with pins both the count and the arg so the OOB
        # fetch can't be silently skipped (a bare all() would pass vacuously on an empty list).
        view._librenms_api.get_device_links.assert_called_once_with(99)
        view._librenms_api.get_ports.assert_called_once_with(99)
        assert result == []  # empty OOB result still flows through as a successful empty refresh

    def test_oob_non_numeric_id_is_coerced_not_passed_raw(self):
        """A non-numeric stored OOB id must fail closed (coerced to None), never reach get_device_links/get_ports as a garbage device URL."""
        view = self._make_view()
        obj = _mock_obj()
        obj.consoleserverports.exists.return_value = False
        view._librenms_api.get_librenms_id.return_value = None  # OOB-only: no host id to fetch
        view._librenms_api.get_device_links.return_value = (True, {"links": []})
        view._librenms_api.get_ports.return_value = (True, {"ports": []})

        with (
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="k"),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_interface_name_field",
                return_value="ifName",
            ),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=obj),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_oob",
                return_value={"id": "not-a-number"},
            ),
        ):
            mock_cache.get.return_value = None
            view.get_links_data(obj)

        # The garbage id must never reach a device-scoped LibreNMS call: that would build
        # GET /devices/not-a-number/... → 404 and silently drop the OOB rows. coerce_librenms_id
        # rejects it exactly like the host id is coerced one block above. This is OOB-only (host id
        # is None too), so the fail-closed contract is the strongest form — NO device-scoped call at
        # all, which also catches a regression that passes None (get_ports(None) → GET /devices/None).
        view._librenms_api.get_device_links.assert_not_called()
        view._librenms_api.get_ports.assert_not_called()

    def test_oob_corrupt_id_flags_fetch_failed_not_silently_dropped(self):
        """A linked OOB controller with a corrupt id must surface the failure, not silently drop it."""
        view = self._make_view()
        obj = _mock_obj()
        obj.consoleserverports.exists.return_value = False
        view._librenms_api.get_librenms_id.return_value = None  # OOB-only: no host id
        view._librenms_api.get_device_links.return_value = (True, {"links": []})
        view._librenms_api.get_ports.return_value = (True, {"ports": []})

        with (
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="k"),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_interface_name_field",
                return_value="ifName",
            ),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device", return_value=obj),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_oob",
                return_value={"id": "not-a-number"},  # OOB IS linked, but its id is corrupt
            ),
        ):
            mock_cache.get.return_value = None
            view.get_links_data(obj)

        # Unfixed: _merge_oob_cable_links returns False (looks like "no OOB") and never flags, so
        # post() shows a "successful" banner while the OOB rows silently vanish. Fixed: the
        # linked-but-corrupt case flags the failure so the user is warned.
        assert view._oob_links_fetch_failed is True

    def test_oob_only_failed_oob_fetch_returns_none_not_empty(self):
        """The OOB-scoped exemption holds ONLY when the OOB fetch succeeded."""
        view = self._make_view()
        obj = _mock_obj()
        obj.consoleserverports.exists.return_value = False

        view._librenms_api.get_librenms_id.return_value = None

        def _links(dev_id):
            if dev_id is None:  # host fetch — no host mapping
                return (False, {"error": "Device not found in LibreNMS"})
            return (False, {"error": "OOB controller unreachable"})  # OOB fetch FAILS

        view._librenms_api.get_device_links.side_effect = _links
        view._librenms_api.get_ports.return_value = (True, {"ports": []})

        with (
            patch.object(view, "get_ports_data", return_value={"ports": []}),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_interface_name_field",
                return_value="ifName",
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device",
                return_value=obj,
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_oob",
                return_value={"id": 99},
            ),
        ):
            result = view.get_links_data(obj)

        # Failed OOB fetch ⇒ None (not []), so the stale cache snapshot survives.
        assert result is None
        # The failure flag was set by the OOB branch, which is what disqualifies the exemption.
        assert view._oob_links_fetch_failed is True

    def test_oob_only_malformed_links_payload_returns_none(self):
        """OOB fetch SUCCEEDS but returns a malformed links payload (links not a list)."""
        view = self._make_view()
        obj = _mock_obj()
        obj.consoleserverports.exists.return_value = False

        view._librenms_api.get_librenms_id.return_value = None

        def _links(dev_id):
            if dev_id is None:  # host fetch — no host mapping
                return (False, {"error": "Device not found in LibreNMS"})
            return (True, {"links": None})  # OOB fetch OK, but links payload is malformed (not a list)

        view._librenms_api.get_device_links.side_effect = _links
        view._librenms_api.get_ports.return_value = (True, {"ports": []})

        with (
            patch.object(view, "get_ports_data", return_value={"ports": []}),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_interface_name_field",
                return_value="ifName",
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device",
                return_value=obj,
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_oob",
                return_value={"id": 99},
            ),
        ):
            result = view.get_links_data(obj)

        # Malformed OOB links ⇒ None (not []), so the cache isn't cleared by a degraded refresh.
        assert result is None
        assert view._oob_links_fetch_failed is True

    def test_failed_host_fetch_records_message_only_error(self):
        """A failed host links fetch may carry its detail under "message" (no "error" key)."""
        view = self._make_view()
        obj = _mock_obj()
        obj.consoleserverports.exists.return_value = False
        view._librenms_api.get_librenms_id.return_value = 42  # host mapping present

        # Host fetch fails with a message-only body.
        view._librenms_api.get_device_links.return_value = (False, {"message": "Device is down"})
        view._librenms_api.get_ports.return_value = (True, {"ports": []})

        with (
            patch.object(view, "get_ports_data", return_value={"ports": []}),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_interface_name_field",
                return_value="ifName",
            ),
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device",
                return_value=obj,
            ),
            patch("netbox_librenms_plugin.views.base.cables_view.get_librenms_oob", return_value=None),
        ):
            view.get_links_data(obj)

        assert view._links_fetch_error == "Device is down"


# =============================================================================
# TestGetDeviceByIdOrNameEdgeCases  — MultipleObjectsReturned, FQDN fallback
# =============================================================================


@pytest.mark.django_db
class TestGetDeviceByIdOrNameEdgeCases:
    """Real-DB tests for get_device_by_id_or_name edge cases."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_multiple_objects_returned_for_librenms_id(self):
        """Two devices sharing librenms_id 42 → MultipleObjectsReturned → (None, False, error)."""
        view = self._make_view()
        make_device("dup-id-a", librenms_cf={"default": 42})
        make_device("dup-id-b", librenms_cf={"default": 42})

        device, found, error = view.get_device_by_id_or_name(42, "switch.example.com")

        assert device is None
        assert found is False
        assert error is not None
        assert "42" in error

    def test_fqdn_fails_simple_hostname_succeeds(self):
        """FQDN not found; the short hostname (before the first dot) resolves to a real device."""
        view = self._make_view()
        dev = make_device("switch")  # only the short name exists

        device, found, error = view.get_device_by_id_or_name(None, "switch.example.com")

        assert found is True
        assert device == dev
        assert error is None


# =============================================================================
# TestEnrichLocalPortVC  — VC member path in enrich_local_port (line 174)
# =============================================================================


@pytest.mark.django_db
class TestEnrichLocalPortVC:
    """Real-DB test for enrich_local_port when obj.virtual_chassis is truthy."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_vc_path_resolves_member_interface(self):
        """VC device → the port's slot number selects the member; its interface URL is set."""
        view = self._make_view()
        master, member = _vc_with_member("vc-elp", "elp-master", "elp-member", member_pos=1)
        iface = make_interface(member, "Gi1/0/0")  # "Gi1/..." → vc_position 1 → member

        link = {"local_port": "Gi1/0/0", "local_port_id": 10}
        view.enrich_local_port(link, master)

        assert link.get("netbox_local_interface_id") == iface.pk
        assert link.get("local_port_url", "").endswith(f"/dcim/interfaces/{iface.pk}/")


# =============================================================================
# TestEnrichRemotePort  — VC and non-VC paths (lines 190-227)
# =============================================================================


@pytest.mark.django_db
class TestEnrichRemotePort:
    """Real-DB tests for enrich_remote_port VC and non-VC paths."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_vc_path_finds_by_librenms_id(self):
        """VC device: remote interface found on the slot member by librenms_id."""
        view = self._make_view()
        master, member = _vc_with_member("vc-erp1", "erp1-master", "erp1-member", member_pos=1)
        iface = make_interface(member, "Gi1/0/1")
        _seed_lib_id(iface, 20)

        # remote_port keeps the "Gi1/" prefix (so member selection still resolves slot 1) but
        # differs from iface.name, so the name fallback CANNOT mask a broken librenms_id lookup —
        # only the id path can reach this interface.
        link = {"remote_port": "Gi1/0/99", "remote_port_id": 20}
        result = view.enrich_remote_port(link, master)

        assert result["netbox_remote_interface_id"] == iface.pk
        assert result["remote_port_url"].endswith(f"/dcim/interfaces/{iface.pk}/")
        assert result["remote_port_name"] == "Gi1/0/1"

    def test_vc_path_falls_back_to_name_when_librenms_id_miss(self):
        """VC device: librenms_id lookup misses (no CF) → falls back to name match."""
        view = self._make_view()
        master, member = _vc_with_member("vc-erp2", "erp2-master", "erp2-member", member_pos=1)
        iface = make_interface(member, "Gi1/0/2")  # no librenms_id seeded

        link = {"remote_port": "Gi1/0/2", "remote_port_id": 20}  # id 20 matches nothing
        result = view.enrich_remote_port(link, master)

        assert result["netbox_remote_interface_id"] == iface.pk

    def test_vc_path_leaves_interface_unresolved_when_member_position_is_missing(self):
        """A failed VC-position lookup must not resolve the advertised port against the selected member."""
        view = self._make_view()
        master, member = _vc_with_member("vc-erp-missing", "erp-missing-master", "erp-missing-member", member_pos=1)
        # LibreNMS advertises position 2, which is absent. A same-named interface on the selected
        # position-1 device must not be mistaken for the missing member's remote endpoint.
        make_interface(member, "Gi2/0/1")
        make_interface(master, "Gi2/0/1")

        result = view.enrich_remote_port({"remote_port": "Gi2/0/1", "remote_port_id": 99}, master)

        assert "netbox_remote_interface_id" not in result
        assert "remote_port_url" not in result

    def test_non_vc_path_finds_by_librenms_id(self):
        """Non-VC device: remote interface found by librenms_id."""
        view = self._make_view()
        device = make_device("erp-nonvc-id")
        iface = make_interface(device, "eth0")
        _seed_lib_id(iface, 15)

        # remote_port deliberately differs from iface.name so only the librenms_id lookup can
        # match — the name fallback (filter(name="remote-eth0")) resolves nothing.
        link = {"remote_port": "remote-eth0", "remote_port_id": 15}
        result = view.enrich_remote_port(link, device)

        assert result["netbox_remote_interface_id"] == iface.pk
        assert result["remote_port_url"].endswith(f"/dcim/interfaces/{iface.pk}/")
        assert result["remote_port_name"] == "eth0"

    def test_non_vc_path_falls_back_to_name(self):
        """Non-VC device: librenms_id lookup misses → falls back to name match."""
        view = self._make_view()
        device = make_device("erp-nonvc-name")
        iface = make_interface(device, "eth1")  # no librenms_id seeded

        link = {"remote_port": "eth1", "remote_port_id": 15}  # id matches nothing
        result = view.enrich_remote_port(link, device)

        assert result["netbox_remote_interface_id"] == iface.pk

    def test_no_remote_port_key_returns_link_unchanged(self):
        """When link has no 'remote_port', enrichment is skipped but the link is returned (never None) so reassigning callers don't NoneType-crash."""
        view = self._make_view()
        device = make_device("erp-nokey")
        link = {}  # No remote_port key

        result = view.enrich_remote_port(link, device)
        assert result is link
        assert "remote_port_url" not in result

    def test_interface_not_found_does_not_set_url(self):
        """When no remote interface matches by id or name, url/id keys are not set."""
        view = self._make_view()
        device = make_device("erp-nomatch")
        make_interface(device, "eth9")  # present but not referenced

        link = {"remote_port": "eth2", "remote_port_id": 99}
        result = view.enrich_remote_port(link, device)

        assert "remote_port_url" not in result
        assert "netbox_remote_interface_id" not in result


# =============================================================================
# TestProcessRemoteDevice  — found=True and found=False paths (lines 264-283)
# =============================================================================


@pytest.mark.django_db
class TestProcessRemoteDevice:
    """Real-DB tests for process_remote_device found=True and found=False paths."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        return view

    def test_found_true_sets_remote_device_url(self):
        """A real remote device resolved by name → remote_device_url + id set (real reverse)."""
        view = self._make_view()
        remote = make_device("switch-b")

        link = {"remote_port": "Gi0/1", "remote_port_id": None}
        result = view.process_remote_device(link, "switch-b", None)

        assert result["netbox_remote_device_id"] == remote.pk
        assert result["remote_device_url"].endswith(f"/dcim/devices/{remote.pk}/")

    def test_found_false_with_error_message(self):
        """Two devices share the name → ambiguity error surfaces as cable_status."""
        from dcim.models import Device, Site

        view = self._make_view()
        d1 = make_device("switch-b")
        site2 = Site.objects.create(name="prd-site2", slug="prd-site2")
        Device.objects.create(name="switch-b", device_type=d1.device_type, role=d1.role, site=site2, status="active")

        link = {"remote_port": "Gi0/1", "remote_port_id": None}
        result = view.process_remote_device(link, "switch-b", None)

        assert "Multiple devices found" in result["cable_status"]
        assert result["can_create_cable"] is False

    def test_found_false_without_error_message_uses_default(self):
        """No device matches → cable_status = 'Device Not Found in NetBox'."""
        view = self._make_view()
        make_device("some-other-device")

        link = {"remote_port": "Gi0/1", "remote_port_id": None}
        result = view.process_remote_device(link, "switch-b", None)

        assert result["cable_status"] == "Device Not Found in NetBox"
        assert result["can_create_cable"] is False


class TestCablePostHostFetchWarning:
    """post() must warn when the host LLDP fetch failed but OOB/serial rows still made the refresh look successful — otherwise host cables are silently omitted under a success banner."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view.partial_template_name = "x.html"
        view._librenms_api = MagicMock(server_key="default")
        view.get_object = MagicMock(return_value=MagicMock(pk=1))
        view.rebind_api_for_server = MagicMock(return_value="default")
        return view

    def _run(self, *, links_fetch_error, librenms_id):
        view = self._make_view()
        request = MagicMock()
        request.POST.get.return_value = "default"

        def _prep(*a, **k):
            # Mirror get_links_data recording a host failure while other rows kept it "successful".
            view._links_fetch_error = links_fetch_error
            view.librenms_id = librenms_id
            return {"object": MagicMock(), "table": MagicMock(), "server_key": "default"}

        view._prepare_context = MagicMock(side_effect=_prep)
        with (
            patch("netbox_librenms_plugin.views.base.cables_view.messages") as mock_msgs,
            patch("netbox_librenms_plugin.views.mixins.render"),
            patch("netbox_librenms_plugin.utils.build_migrated_context", return_value={}),
        ):
            view.post(request, pk=1)
        return [c.args[1] for c in mock_msgs.warning.call_args_list]

    def test_warns_on_host_fetch_failure_with_rows(self):
        warn_texts = self._run(links_fetch_error="auth failed", librenms_id=42)
        assert any("host links fetch failed" in t for t in warn_texts)

    def test_no_host_warning_for_oob_only_device(self):
        # librenms_id is None → a host fetch "failure" is expected, not surfaced as a warning.
        warn_texts = self._run(links_fetch_error="device not found", librenms_id=None)
        assert not any("host links fetch failed" in t for t in warn_texts)


class TestCablePartialSnapshotNotCached:
    """A fresh fetch that partially failed (host or OOB) must NOT be cached — later cached renders/verify would otherwise silently serve the incomplete cable set."""

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock(server_key="default", cache_timeout=300)
        view.get_cache_key = MagicMock(return_value="links-key")
        view.get_table = MagicMock(return_value=MagicMock())
        view.enrich_links_data = MagicMock(side_effect=lambda d, o, server_key=None: d)
        return view

    def _links_cache_sets(self, *, oob_failed, links_error, librenms_id):
        view = self._make_view()

        def fake_get_links(obj, server_key=None, sync_device=None):
            view._oob_links_fetch_failed = oob_failed
            view._links_fetch_error = links_error
            view.librenms_id = librenms_id
            return [{"local_port": "Gi0/0", "remote_port": "Gi0/1", "_source": "host"}]

        view.get_links_data = MagicMock(side_effect=fake_get_links)
        with (
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
            patch(
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device",
                return_value=MagicMock(),
            ),
        ):
            mock_cache.ttl.return_value = None
            view._prepare_context(MagicMock(), MagicMock(virtual_chassis=None), fetch_fresh=True, server_key="default")
        return [c for c in mock_cache.set.call_args_list if c.args and c.args[0] == "links-key"]

    def test_oob_fetch_failure_not_cached(self):
        assert self._links_cache_sets(oob_failed=True, links_error=None, librenms_id=42) == []

    def test_host_fetch_failure_with_host_id_not_cached(self):
        assert self._links_cache_sets(oob_failed=False, links_error="auth failed", librenms_id=42) == []

    def test_oob_only_mapping_still_cached(self):
        # librenms_id None + a host "failure" is an OOB-only mapping (absent host) → still cache it.
        assert len(self._links_cache_sets(oob_failed=False, links_error="no host", librenms_id=None)) == 1

    def test_clean_fresh_fetch_cached(self):
        assert len(self._links_cache_sets(oob_failed=False, links_error=None, librenms_id=42)) == 1


# =============================================================================
# TestGetTableOverride  — BaseCableTableView.get_table (lines 302-305)
# =============================================================================


class TestCableTableHtmxUrl:
    """_prepare_context sets table.htmx_url from the RESOLVED server scope.

    Set in _prepare_context (not a base get_table override): DeviceCableTableView
    overrides get_table without calling super, so a base override never ran for the
    device tab — and the base's lazy self.librenms_api.server_key could point at a
    different server than the resolved scope.
    """

    def _run_prepare(self, server_key, path="/cables/"):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "session-server"  # must NOT leak into the URL
        view.request = _mock_request(path)

        mock_table = MagicMock()
        with (
            patch.object(view, "get_cache_key", return_value="cable-key"),
            patch.object(view, "enrich_links_data", return_value=[]),
            patch.object(view, "get_table", return_value=mock_table),
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
        ):
            mock_cache.get.return_value = {"links": []}
            mock_cache.ttl.return_value = 300
            result = view._prepare_context(view.request, MagicMock(), fetch_fresh=False, server_key=server_key)
        assert result is not None
        return mock_table

    def test_sets_htmx_url_with_resolved_server_key(self):
        table = self._run_prepare("secondary")
        assert table.htmx_url == "/cables/?tab=cables&server_key=secondary"

    def test_htmx_url_without_server_key_when_scope_and_session_are_blank(self):
        """No resolved key AND a blank session client -> no server_key parameter."""
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = None
        view.request = _mock_request("/cables/")

        mock_table = MagicMock()
        with (
            patch.object(view, "get_cache_key", return_value="cable-key"),
            patch.object(view, "enrich_links_data", return_value=[]),
            patch.object(view, "get_table", return_value=mock_table),
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
        ):
            mock_cache.get.return_value = {"links": []}
            mock_cache.ttl.return_value = 300
            result = view._prepare_context(view.request, MagicMock(), fetch_fresh=False, server_key=None)

        assert result is not None
        assert mock_table.htmx_url == "/cables/?tab=cables"


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
        # dispatch() sets self.request in production; tests call post() directly, so set an
        # authorized request here for the object-permission gate (reads self.request.user).
        view.request = _mock_request()
        view.request.user.has_perm.return_value = True
        return view

    @pytest.mark.django_db
    def test_unconfigured_posted_server_key_falls_back_to_session(self):
        """A posted server_key naming no configured server must not scope the links cache / _librenms_id_q ORM lookups — fall back to the active server."""
        import json

        view = self._make_view()
        view.request.user = _authorized_superuser("srvkey")
        view._librenms_api.server_key = "good"
        device = _real_cable_device("srvkey")  # non-VC → primary_device = selected_device

        mock_request = MagicMock()
        mock_request.body = json.dumps({"device_id": device.pk, "local_port_id": 10, "server_key": "ghost"}).encode()

        captured = {}

        def fake_cache_key(dev, kind, sk):
            captured["server_key"] = sk
            return "ck"

        with (
            # "ghost" is not configured → the view must fall back to the active-server key.
            patch(
                "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
                return_value={"good": "Good"},
            ),
            patch("netbox_librenms_plugin.views.base.cables_view.cache") as mock_cache,
            patch.object(view, "get_cache_key", side_effect=fake_cache_key),
        ):
            mock_cache.get.return_value = None  # no cached links → exits after building the (validated) key
            view.post(mock_request)

        assert captured["server_key"] == "good"  # "ghost" is unconfigured → session key used

    @pytest.mark.django_db
    def test_vc_member_resolution_calls_get_virtual_chassis_member(self):
        """VC device → get_virtual_chassis_member called with device and local_port."""
        import json

        view = self._make_view()
        view.request.user = _authorized_superuser("vcmember")
        device = _real_cable_device("vcmember", vc=True)  # real VC device

        mock_request = MagicMock()
        mock_request.body = json.dumps(
            {
                "device_id": device.pk,
                "local_port_id": 10,
                "server_key": "default",
            }
        ).encode()

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
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device",
                return_value=device,
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

        mock_vc.assert_called_once_with(device, "Gi0/0")
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
        # dispatch() sets self.request in production; tests call post() directly, so set an
        # authorized request here for the object-permission gate (reads self.request.user).
        view.request = _mock_request()
        view.request.user.has_perm.return_value = True
        return view

    @pytest.mark.django_db
    def test_interface_not_found_fills_formatted_row(self):
        """When no local interface found, formatted_row reflects missing interface."""
        import json as json_mod

        view = self._make_view()
        view.request.user = _authorized_superuser("ifnotfound")
        device = _real_cable_device("ifnotfound")  # non-VC, no interfaces → local lookup returns None

        mock_request = MagicMock()
        mock_request.body = json_mod.dumps(
            {
                "device_id": device.pk,
                "local_port_id": 10,
                "server_key": "default",
            }
        ).encode()

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
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device",
                return_value=device,
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

    @pytest.mark.django_db
    def test_cable_url_present_wraps_cable_status_in_anchor(self):
        """When cable_url is in link_data, cable_status is wrapped in an <a> tag (line 514)."""
        import json as json_mod

        view = self._make_view()
        view.request.user = _authorized_superuser("cableurl")
        device = _real_cable_device("cableurl", bound_port_id=10)  # local interface bound to librenms id 10

        mock_request = MagicMock()
        mock_request.body = json_mod.dumps(
            {
                "device_id": device.pk,
                "local_port_id": 10,
                "server_key": "default",
            }
        ).encode()

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
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device",
                return_value=device,
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

    @pytest.mark.parametrize(
        "payload",
        [None, {"port": []}, {"port": ["bad"]}, "not-a-dict", {"port": "x"}],
    )
    def test_get_port_info_caches_none_for_malformed_payload(self, payload):
        """Issue #111: a truthy success with a malformed get_port_by_id payload must cache None without raising (None -> 'port' in None TypeError; ['bad'] -> non-dict row that later crashes at port_info.get())."""
        view = self._make_view()
        view._librenms_api.get_port_by_id.return_value = (True, payload)
        cache = {}

        result = view._get_port_info(7, cache, "ifName")

        assert result is None
        assert cache[7] is None

    def test_get_port_info_caches_first_dict_row(self):
        """A well-formed payload caches the first port dict."""
        view = self._make_view()
        row = {"ifName": "eth0", "port_id": 7}
        view._librenms_api.get_port_by_id.return_value = (True, {"port": [row]})
        cache = {}

        assert view._get_port_info(7, cache, "ifName") == row
        assert cache[7] == row

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
        from netbox_librenms_plugin.views.base.ip_addresses_view import BaseIPAddressTableView

        allowed = make_device("getobj-allowed-ip")
        hidden = make_device("getobj-hidden-ip")
        user = make_user_with_perms("getobj-viewer-ip", [("view", Device)], constraints={"name": allowed.name})
        view = make_view(BaseIPAddressTableView, make_request("get", user=user))
        view.model = Device

        assert view.get_object(allowed.pk).pk == allowed.pk
        with pytest.raises(Http404):
            view.get_object(hidden.pk)

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

    def test_get_ip_addresses_coerces_poisoned_id(self):
        """A poisoned cached librenms_id fails closed before the HTTP fetch."""
        view = self._make_view()
        # bool is the canonical poison: int(True) == 1 would otherwise look valid.
        view._librenms_api.get_librenms_id.return_value = True

        result = view.get_ip_addresses(_mock_obj())

        view._librenms_api.get_device_ips.assert_not_called()
        assert result == (False, "Device not found in LibreNMS")
        assert view.librenms_id is None


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

    def test_cached_render_coerces_poisoned_stored_id(self):
        """On the cached render with no stored mgmt_ip, a poisoned stored librenms_id is coerced to None so the live mgmt-IP lookup (get_device_info) is never hit."""
        view = self._make_view()
        # The device-id cache path returns its value verbatim — a poisoned bool.
        view._librenms_api.get_stored_librenms_id.return_value = True
        view._librenms_api.cache_timeout = 300

        obj = _mock_obj()
        request = _mock_request()
        # Cached IP rows present but NO mgmt_ip key → drives the cached_mgmt_ip_missing backfill.
        cached = {"ip_addresses": [{"port_id": 1, "ip_address": "10.0.0.1", "prefix_length": 24}]}

        with (
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="ck"),
            patch.object(view, "enrich_ip_data", return_value=[]),
            patch.object(view, "get_table", return_value=MagicMock()),
        ):
            mock_cache.get.return_value = cached
            mock_cache.ttl.return_value = None
            view._prepare_context(request, obj, "ifName", fetch_fresh=False, server_key="default")

        # Poisoned True → coerced to None → _resolve_management_ip bails before any HTTP call.
        assert view.librenms_id is None
        view._librenms_api.get_device_info.assert_not_called()

    def test_non_dict_cached_entry_drops_to_none_not_500(self):
        """A stale/corrupt non-dict cached entry (e.g. a list from a legacy snapshot shape) must drop to None and render empty, not 500 on a .get() against a list — mirrors the interfaces/modules cached-path isinstance guard."""
        from uuid import uuid4

        from django.core.cache import cache

        view = self._make_view()

        obj = _mock_obj()
        request = _mock_request()
        key = f"nblp-test-corrupt-{uuid4().hex}"

        cache.set(key, ["not", "a", "dict"])
        try:
            with patch.object(view, "get_cache_key", return_value=key):
                result = view._prepare_context(request, obj, "ifName", fetch_fresh=False)

            assert result is None
            assert cache.get(key) is None
        finally:
            cache.delete(key)

    def test_prepare_context_uses_request_object_interface_name_fallback(self):
        """A missing explicit name field must use the request and object preference."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()
        # The cached field differs from the resolver's, so only the resolved value can
        # satisfy the assertion below.
        cached = {
            "ip_addresses": [],
            "mgmt_ip": "",
            "ports_by_id": {},
            "interface_name_field": "ifName",
        }

        with (
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="test-key"),
            patch(
                "netbox_librenms_plugin.views.base.ip_addresses_view.get_interface_name_field",
                return_value="ifDescr",
            ) as get_name_field,
            patch.object(view, "enrich_ip_data", return_value=[]) as enrich,
            patch.object(view, "get_table", return_value=MagicMock()),
        ):
            mock_cache.get.return_value = cached
            mock_cache.ttl.return_value = None
            result = view._prepare_context(request, obj, None, fetch_fresh=False, server_key="default")

        assert result is not None
        get_name_field.assert_called_once_with(request, obj)
        assert enrich.call_args.args[2] == "ifDescr"

    def test_fetch_fresh_malformed_ip_payload_returns_none(self):
        """A success flag with a non-list get_ip_addresses() payload (or a list with non-dict entries) must be treated as a fetch failure — return None before enrichment so post() neither renders an empty table under a success banner nor caches the empty snapshot."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        with (
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="ck"),
            patch.object(view, "get_ip_addresses", return_value=(True, {"unexpected": "dict"})),
            patch.object(view, "_resolve_management_ip", return_value="") as mock_mgmt,
            patch.object(view, "enrich_ip_data") as mock_enrich,
        ):
            result = view._prepare_context(request, obj, "ifName", fetch_fresh=True)

        assert result is None
        mock_mgmt.assert_not_called()  # bail before the live mgmt-ip lookup
        mock_enrich.assert_not_called()  # never enrich a malformed payload
        mock_cache.set.assert_not_called()  # never cache the empty snapshot as complete
        # ...and purge any prior valid snapshot so the fail-closed takes effect (no stale rows
        # served on the next GET until TTL).
        mock_cache.delete.assert_any_call("ck")

    def test_fetch_fresh_dict_row_missing_ip_fields_returns_none(self):
        """A dict row that passes the container-shape check but lacks the address/prefix and port_id fields _create_base_ip_entry() reads would KeyError mid-enrichment and 500 the fresh-refresh path."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        with (
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="ck"),
            # Well-formed list of dicts, but the single row is missing the IP/prefix fields.
            patch.object(view, "get_ip_addresses", return_value=(True, [{"port_id": 7}])),
            patch.object(view, "_resolve_management_ip", return_value="") as mock_mgmt,
            patch.object(view, "enrich_ip_data") as mock_enrich,
        ):
            result = view._prepare_context(request, obj, "ifName", fetch_fresh=True)

        assert result is None
        mock_mgmt.assert_not_called()  # bail before the live mgmt-ip lookup
        mock_enrich.assert_not_called()  # never enrich a row that would KeyError downstream
        mock_cache.set.assert_not_called()

    def test_fetch_fresh_unhashable_port_id_returns_none(self):
        """A row with a valid address/prefix pair but an unhashable port_id (e.g. {}) must fail closed: as a cache-dict key in _get_port_info() it would raise `unhashable type` and 500 the fresh-refresh path."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        with (
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="ck"),
            patch.object(
                view,
                "get_ip_addresses",
                return_value=(True, [{"port_id": {}, "ip_address": "10.0.0.1", "prefix_length": 24}]),
            ),
            patch.object(view, "_resolve_management_ip", return_value="") as mock_mgmt,
            patch.object(view, "enrich_ip_data") as mock_enrich,
        ):
            result = view._prepare_context(request, obj, "ifName", fetch_fresh=True)

        assert result is None
        mock_mgmt.assert_not_called()  # bail before the live mgmt-ip lookup
        mock_enrich.assert_not_called()  # never enrich a row whose port_id would crash the cache lookup
        mock_cache.set.assert_not_called()

    def test_cached_render_reuses_cached_ports_without_live_calls(self):
        """A warm-cache render must enrich from the cached ports_by_id map and never call get_port_by_id(), so the IP tab keeps working when LibreNMS is unavailable."""
        view = self._make_view()
        obj = _mock_obj()
        request = _mock_request()

        cached_payload = {
            "ip_addresses": [{"ip_address": "10.0.0.5", "prefix_length": 32, "port_id": 5}],
            "mgmt_ip": "10.0.0.1",
            "ports_by_id": {5: {"ifName": "Gi0/1"}},
        }
        with (
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="ck"),
            patch.object(
                view,
                "_prefetch_netbox_data",
                return_value={
                    "interfaces_by_librenms_id": {},
                    "interfaces_by_name": {},
                    "all_interfaces": [],
                    "device": obj,
                    "ip_addresses_map": {},
                    "vrfs": [],
                },
            ),
            patch.object(view, "get_table", return_value=MagicMock()) as mock_get_table,
        ):
            mock_cache.get.return_value = cached_payload
            mock_cache.ttl.return_value = 100
            view._prepare_context(request, obj, "ifName", fetch_fresh=False)

        view._librenms_api.get_port_by_id.assert_not_called()
        mock_get_table.assert_called_once()
        # The cached port name must actually reach the rendered rows — i.e. enrich_ip_data
        # used ports_by_id to set interface_name. Asserting only "no live calls" would still
        # pass if a regression rendered the row without the cached name.
        enriched_rows = mock_get_table.call_args.args[0]
        assert enriched_rows[0]["interface_name"] == "Gi0/1"

    def test_cache_hit_backfills_missing_ports_by_id(self):
        """A pre-upgrade cache entry without ports_by_id: enrich rebuilds the port map via live get_port_by_id(), and we backfill it into cache under the *remaining* TTL so subsequent warm renders stop re-hitting LibreNMS until the entry would have expired."""
        view = self._make_view()
        view._librenms_api.cache_timeout = 300
        view._librenms_api.get_port_by_id.return_value = (True, {"port": [{"ifName": "Gi0/1"}]})
        obj = _mock_obj()
        request = _mock_request()

        cached_payload = {  # NO ports_by_id key → pre-upgrade entry
            "ip_addresses": [{"ip_address": "10.0.0.5", "prefix_length": 32, "port_id": 5}],
            "mgmt_ip": "10.0.0.1",
        }
        with (
            patch("netbox_librenms_plugin.views.base.ip_addresses_view.cache") as mock_cache,
            patch.object(view, "get_cache_key", return_value="ck"),
            patch.object(
                view,
                "_prefetch_netbox_data",
                return_value={
                    "interfaces_by_librenms_id": {},
                    "interfaces_by_name": {},
                    "all_interfaces": [],
                    "device": obj,
                    "ip_addresses_map": {},
                    "vrfs": [],
                },
            ),
            patch.object(view, "get_table", return_value=MagicMock()),
        ):
            mock_cache.get.return_value = cached_payload
            mock_cache.ttl.return_value = 120
            view._prepare_context(request, obj, "ifName", fetch_fresh=False)

        # The rebuilt port map is written back under the remaining TTL (not the full timeout).
        mock_cache.set.assert_called_once()
        args, kwargs = mock_cache.set.call_args
        assert args[0] == "ck"
        assert args[1]["ports_by_id"] == {5: {"ifName": "Gi0/1"}}
        assert kwargs["timeout"] == 120


# =============================================================================
# TestSingleIPAddressVerifyViewGetObject  — _get_object (lines 325-339)
# =============================================================================


@pytest.mark.django_db
class TestSingleIPAddressVerifyViewGetObject:
    """_get_object resolves the right real object by type (and untyped), scoped to the caller's perms.

    Rewritten from get_object_or_404/Device.objects.filter call-shape mocks to real Device/VM rows:
    the object-scoping added in this PR routes the lookup through ``Model.objects.restrict`` and reads
    ``self.request.user``, which the old call-signature assertions never exercised.
    """

    def _view(self):
        from django.contrib.auth import get_user_model
        from django.test import RequestFactory

        from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

        view = object.__new__(SingleIPAddressVerifyView)
        request = RequestFactory().get("/")
        request.user = get_user_model().objects.create_superuser(username="ipverify-getobj", email="", password="x")
        view.request = request
        return view

    def _device(self, name="ipverify-dev"):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

        mfr, _ = Manufacturer.objects.get_or_create(name="IPV-Mfr", slug="ipv-mfr")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="IPV-DT", slug="ipv-dt")
        role, _ = DeviceRole.objects.get_or_create(name="IPV-Role", slug="ipv-role")
        site, _ = Site.objects.get_or_create(name="IPV-Site", slug="ipv-site")
        return Device.objects.create(name=name, device_type=dt, role=role, site=site, status="active")

    def _vm(self, name="ipverify-vm"):
        from virtualization.models import Cluster, ClusterType, VirtualMachine

        ct, _ = ClusterType.objects.get_or_create(name="IPV-CT", slug="ipv-ct")
        cluster, _ = Cluster.objects.get_or_create(name="IPV-Cluster", type=ct)
        return VirtualMachine.objects.create(name=name, cluster=cluster, status="active")

    def test_device_type_resolves_real_device(self):
        view = self._view()
        device = self._device()
        assert view._get_object(device.pk, "device").pk == device.pk

    def test_vm_type_resolves_real_vm(self):
        view = self._view()
        vm = self._vm()
        assert view._get_object(vm.pk, "virtualmachine").pk == vm.pk

    def test_no_type_finds_device(self):
        view = self._view()
        device = self._device("ipverify-dev-nt")
        assert view._get_object(device.pk, None).pk == device.pk

    def test_no_type_device_absent_finds_vm(self):
        view = self._view()
        vm = self._vm("ipverify-vm-nt")
        # No Device exists with this pk, so the device lookup misses and the VM lookup resolves.
        assert view._get_object(vm.pk, None).pk == vm.pk

    def test_no_type_neither_found_raises_http404(self):
        from django.http import Http404

        view = self._view()
        with pytest.raises(Http404):
            view._get_object(2_147_483_647, None)


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
        """An invalid prefix is rejected by the shared IP parser."""
        view = self._make_view()
        with pytest.raises(ValueError):
            view._parse_ip_address("192.168.1.1/abc")

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
        entry = {
            "ip_address": "192.168.1.1",
            "ip_with_mask": "192.168.1.1/24",
            "prefix_length": 24,
            "vrf_id": 5,
            "port_id": 10,
        }
        cached = {"ip_addresses": [entry]}
        ip_entry, vrf_id, port_id = view._find_in_cache(cached, "192.168.1.1", 24)
        assert ip_entry is entry
        assert vrf_id == 5
        assert port_id == 10

    def test_no_match_returns_triple_none(self):
        """Entries present but no match → (None, None, None)."""
        view = self._make_view()
        entry = {
            "ip_address": "10.0.0.1",
            "ip_with_mask": "10.0.0.1/16",
            "prefix_length": 16,
            "vrf_id": None,
            "port_id": 1,
        }
        cached = {"ip_addresses": [entry]}
        result = view._find_in_cache(cached, "192.168.1.1", 24)
        assert result == (None, None, None)


# =============================================================================
# TestSingleIPAddressVerifyViewFindExistingIp  — _find_existing_ip (lines 373-387)
# =============================================================================


@pytest.mark.django_db
class TestSingleIPAddressVerifyViewFindExistingIp:
    """Real-DB tests for SingleIPAddressVerifyView._find_existing_ip.

    _find_existing_ip queries the real IPAddress model the plugin owns, so these exercise the
    actual ORM lookup (address + vrf scoping). Mocking IPAddress.objects here left the exact
    filter kwargs unverified — a change to the lookup fields would pass while the real query broke.
    """

    def _make_view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

        return object.__new__(SingleIPAddressVerifyView)

    def test_ip_not_found_returns_false_false_none(self):
        """An address absent from NetBox → (False, False, None)."""
        view = self._make_view()

        assert view._find_existing_ip("192.168.1.1", 24, vrf_id=None) == (False, False, None)

    def test_ip_found_global_vrf(self):
        """A real global (no-VRF) IPAddress is found and reported as existing in the global VRF."""
        from ipam.models import IPAddress

        ip = IPAddress.objects.create(address="10.0.0.1/8")
        view = self._make_view()

        exists_any, exists_vrf, url = view._find_existing_ip("10.0.0.1", 8, vrf_id=None)

        assert exists_any is True
        assert exists_vrf is True
        assert url == ip.get_absolute_url()

    def test_ip_found_specific_vrf(self):
        """A real IPAddress in a specific VRF is matched when that vrf_id is queried."""
        from ipam.models import VRF, IPAddress

        vrf = VRF.objects.create(name="cr116-vrf")
        ip = IPAddress.objects.create(address="192.168.1.1/24", vrf=vrf)
        view = self._make_view()

        exists_any, exists_vrf, url = view._find_existing_ip("192.168.1.1", 24, vrf_id=vrf.pk)

        assert exists_any is True
        assert exists_vrf is True
        assert url == ip.get_absolute_url()

    def test_ip_in_vrf_not_matched_as_global(self):
        """An IP that exists only inside a VRF is present but NOT in the global VRF (vrf__isnull=True)."""
        from ipam.models import VRF, IPAddress

        vrf = VRF.objects.create(name="cr116-vrf2")
        IPAddress.objects.create(address="172.16.0.1/24", vrf=vrf)
        view = self._make_view()

        exists_any, exists_vrf, _url = view._find_existing_ip("172.16.0.1", 24, vrf_id=None)

        assert exists_any is True
        assert exists_vrf is False


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
        # Direct post() calls bypass dispatch() (which sets self.request), so null the object-perm
        # gate; the gate itself is covered by TestSingleIPAddressVerifyObjectPermissionGate (real DB).
        view.require_object_permissions_json = MagicMock(return_value=None)
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

    def test_interface_from_device_used_when_cache_has_no_port_id(self):
        """When cache contains an IP entry with no port_id, first device interface is used."""
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
        # And that interface's name/url must actually flow into the rendered record,
        # not just be queried.
        rendered_record = mock_table_instance.render_status.call_args[0][1]
        assert rendered_record["interface_name"] == "eth0"
        assert rendered_record["interface_url"] == "/interface/1/"

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
            # "secondary" must be a configured server for the verify gate to honour it as the cache
            # namespace (an unconfigured/forged key falls back to the active server).
            patch(
                "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
                return_value={"secondary": "Secondary"},
            ),
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


@pytest.mark.django_db
class TestGetDeviceByIdOrNameLine124:
    """DoesNotExist on librenms_id lookup falls through to name lookup (real DB)."""

    def test_librenms_id_doesnotexist_falls_through_to_name(self):
        """remote_device_id 42 matches no device → falls through to the name lookup, which hits."""
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        dev = make_device("switch-a")  # no librenms_id 42 anywhere → id lookup misses, name hits

        device, found, error = view.get_device_by_id_or_name(42, "switch-a")

        assert found is True
        assert device == dev


# =============================================================================
# TestGetDeviceByIdOrNameSimpleHostnameMultiple  — lines 144-145
# =============================================================================


@pytest.mark.django_db
class TestGetDeviceByIdOrNameSimpleHostnameMultiple:
    """MultipleObjectsReturned when searching by simple hostname (real DB)."""

    def test_simple_hostname_multiple_returns_error(self):
        """FQDN not found; the short hostname matches two devices (across sites) → error."""
        from dcim.models import Device, Site

        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        d1 = make_device("switch")  # short name, on the shared site
        site2 = Site.objects.create(name="shmult-site2", slug="shmult-site2")
        Device.objects.create(name="switch", device_type=d1.device_type, role=d1.role, site=site2, status="active")

        device, found, error = view.get_device_by_id_or_name(None, "switch.example.com")

        assert device is None
        assert found is False
        assert error is not None
        assert "switch.example.com" in error


# =============================================================================
# TestEnrichLocalPortVCNameFallback  — line 174 (VC name fallback)
# =============================================================================


@pytest.mark.django_db
class TestEnrichLocalPortVCNameFallback:
    """Real-DB tests for enrich_local_port VC-path name fallback (line 174)."""

    def test_vc_name_fallback_when_librenms_id_miss(self):
        """VC path: librenms_id lookup misses (no CF) → falls back to name lookup."""
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        master, member = _vc_with_member("vc-elpnf1", "elpnf1-m", "elpnf1-mem", member_pos=1)
        iface = make_interface(member, "Gi1/0/0")  # no librenms_id seeded → name fallback

        link = {"local_port": "Gi1/0/0", "local_port_id": 10}  # id 10 matches nothing
        view.enrich_local_port(link, master)

        assert link.get("netbox_local_interface_id") == iface.pk

    def test_vc_no_local_port_id_goes_straight_to_name(self):
        """VC path with local_port_id=None → skips librenms_id, resolves by name."""
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        master, member = _vc_with_member("vc-elpnf2", "elpnf2-m", "elpnf2-mem", member_pos=1)
        iface = make_interface(member, "Gi1/0/0")

        link = {"local_port": "Gi1/0/0", "local_port_id": None}
        view.enrich_local_port(link, master)

        assert link.get("netbox_local_interface_id") == iface.pk


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
        # dispatch() sets self.request in production; tests call post() directly, so set an
        # authorized request here for the object-permission gate (reads self.request.user).
        view.request = _mock_request()
        view.request.user.has_perm.return_value = True
        return view

    @pytest.mark.django_db
    def test_can_create_cable_adds_form_action(self):
        """can_create_cable=True → formatted_row['actions'] contains form."""
        import json as json_mod

        view = self._make_view()
        view.request.user = _authorized_superuser("cancreate")
        device = _real_cable_device("cancreate", bound_port_id=10)  # local interface bound to librenms id 10

        mock_request = MagicMock()
        mock_request.body = json_mod.dumps(
            {
                "device_id": device.pk,
                "local_port_id": 10,
                "server_key": "default",
            }
        ).encode()

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
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device",
                return_value=device,
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
        # dispatch() sets self.request in production; tests call post() directly, so set an
        # authorized request here for the object-permission gate (reads self.request.user).
        view.request = _mock_request()
        view.request.user.has_perm.return_value = True
        return view

    def _run_post(self, view, process_result):
        """Helper: run the post against a real non-VC device (no interface) with a given process_result dict."""
        import json as json_mod

        view.request.user = _authorized_superuser("notfoundbranch")
        device = _real_cable_device("notfoundbranch")  # non-VC, no interface → local lookup returns None

        mock_request = MagicMock()
        mock_request.body = json_mod.dumps(
            {
                "device_id": device.pk,
                "local_port_id": 10,
                "server_key": "default",
            }
        ).encode()

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
                "netbox_librenms_plugin.views.base.cables_view.get_librenms_sync_device",
                return_value=device,
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

    @pytest.mark.django_db
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

    @pytest.mark.django_db
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
