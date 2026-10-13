"""
Database-backed coverage for cable and IP-address base views.

These tests use real NetBox objects, permission checks, cache entries, and rendered JSON.
LibreNMS is not contacted because every exercised view path starts from a warm cache or resolves
only against NetBox state.
"""

import json

import pytest
from django.core.cache import cache
from django.http import Http404

from netbox_librenms_plugin.tests.conftest import (
    cable_together,
    make_device,
    make_interface,
    make_ip,
    make_superuser,
    make_vm,
)
from netbox_librenms_plugin.tests.view_test_helpers import make_request, post as view_post

pytestmark = pytest.mark.django_db


def _request(payload, *, path="/verify/"):
    """Build an authenticated JSON request for a direct view call."""
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return make_request(
        "post",
        body,
        user=make_superuser(),
        path=path,
        content_type="application/json",
    )


def _json(response):
    """Decode a direct JsonResponse call."""
    return json.loads(response.content)


def _seed_librenms_id(obj, value, server_key="default"):
    """Store one real per-server LibreNMS ID mapping."""
    obj.custom_field_data["librenms_id"] = {server_key: value}
    obj.save(update_fields=["custom_field_data"])


def _post_ip(payload):
    """Call the real single-address verification endpoint."""
    from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

    return view_post(SingleIPAddressVerifyView(), _request(payload))


def _post_cable(device, links, *, row_id, **extra):
    """Warm a real cable snapshot and call the real cable verification endpoint."""
    from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

    view = SingleCableVerifyView()
    server_key = view._render_server_key()
    assert server_key is not None
    cache_key = view.get_cache_key(device, "links", server_key)
    cache.set(cache_key, {"links": links}, timeout=300)
    payload = {"device_id": device.pk, "row_id": row_id, "server_key": server_key, **extra}
    return view_post(view, _request(payload))


class TestLibreNMSIdentityQueries:
    """Resolve only valid LibreNMS identities and keep host and OOB identities distinct."""

    @pytest.mark.parametrize("invalid", [True, False, 4.2, 0, -1, "", "   ", None, [], {}])
    def test_invalid_identity_matches_no_device(self, invalid):
        from dcim.models import Device

        from netbox_librenms_plugin.views.base.cables_view import _librenms_id_q

        make_device("id-invalid", librenms_cf={"default": {"id": 42, "oob": {"id": 99}}})

        assert not Device.objects.filter(_librenms_id_q("default", invalid)).exists()

    @pytest.mark.parametrize(
        ("mapping", "server_key", "value"),
        [
            ({"default": 42}, "default", 42),
            ({"default": "42"}, "default", 42),
            ({"default": {"id": 42}}, "default", "42"),
            ({"default": {"oob": {"id": 42}}}, "default", 42),
            (42, "secondary", "42"),
        ],
    )
    def test_supported_storage_shapes_resolve_the_real_device(self, mapping, server_key, value):
        from dcim.models import Device

        from netbox_librenms_plugin.views.base.cables_view import _librenms_id_q

        expected = make_device("id-shape", librenms_cf=mapping)

        assert Device.objects.get(_librenms_id_q(server_key, value)) == expected

    def test_remote_identity_excludes_an_oob_reference(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        expected = make_device("remote-owner", librenms_cf={"default": {"id": 71}})
        make_device("remote-oob-reference", librenms_cf={"default": {"oob": {"id": 71}}})

        device, found, error = BaseCableTableView().get_device_by_id_or_name(
            71,
            "no-name-fallback",
            "default",
        )

        assert (device, found, error) == (expected, True, None)

    def test_exact_and_short_hostname_fallbacks_use_real_querysets(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        expected = make_device("edge-switch")
        view = BaseCableTableView()

        exact = view.get_device_by_id_or_name(None, "edge-switch", "default")
        short = view.get_device_by_id_or_name(None, "edge-switch.example.test", "default")
        missing = view.get_device_by_id_or_name(None, "missing.example.test", "default")

        assert exact == (expected, True, None)
        assert short == (expected, True, None)
        assert missing == (None, False, None)


class TestIPAddressVerifyHelpers:
    """Exercise address verification helpers with real models and realistic cache rows."""

    def _view(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

        view = SingleIPAddressVerifyView()
        view.setup(_request({}))
        return view

    def test_object_lookup_resolves_devices_and_virtual_machines(self):
        device = make_device("ip-object-device")
        vm = make_vm("ip-object-vm")
        view = self._view()

        assert view._get_object(device.pk, "device") == device
        assert view._get_object(vm.pk, "virtualmachine") == vm
        assert view._get_object(device.pk) == device
        with pytest.raises(Http404):
            view._get_object(2_147_483_647)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("198.18.1.9/24", ("198.18.1.9", 24)),
            ("2001:db8::9/64", ("2001:db8::9", 64)),
        ],
    )
    def test_address_parser_preserves_the_host_and_prefix(self, value, expected):
        assert self._view()._parse_ip_address(value) == expected

    @pytest.mark.parametrize("value", ["198.18.1.9", "198.18.1.9/99", "not-an-address/24"])
    def test_address_parser_rejects_incomplete_or_invalid_values(self, value):
        with pytest.raises(ValueError):
            self._view()._parse_ip_address(value)

    @pytest.mark.parametrize(
        "cached",
        [
            None,
            [],
            "legacy",
            {"ip_addresses": [None, "bad", {"ip_with_mask": "invalid"}]},
        ],
    )
    def test_malformed_or_missing_cache_is_a_clean_miss(self, cached):
        assert self._view()._find_in_cache(cached, "198.18.2.8", 24) == (None, None, None)

    def test_cache_match_returns_the_original_vrf_and_port(self):
        entry = {
            "ip_with_mask": "198.18.2.8/24",
            "vrf_id": 17,
            "port_id": 29,
            "interface_name": "Ethernet29",
        }

        match = self._view()._find_in_cache(
            {"ip_addresses": ["bad", entry]},
            "198.18.2.8",
            24,
        )

        assert match == ({**entry, "row_id": entry["ip_with_mask"]}, 17, 29)
        assert "row_id" not in entry

    def test_existing_global_address_returns_its_real_url(self):
        ip = make_ip("198.18.3.1/24")

        exists, in_vrf, url = self._view()._find_existing_ip("198.18.3.1", 24)

        assert exists is True
        assert in_vrf is True
        assert url == ip.get_absolute_url()

    def test_existing_address_in_another_vrf_does_not_match_global(self):
        from ipam.models import VRF

        vrf = VRF.objects.create(name="verify-isolated", rd="64512:8")
        ip = make_ip("198.18.4.1/24")
        ip.vrf = vrf
        ip.save(update_fields=["vrf"])

        exists, in_vrf, url = self._view()._find_existing_ip("198.18.4.1", 24)

        assert exists is True
        assert in_vrf is False
        assert url == ip.get_absolute_url()

    @pytest.mark.parametrize(
        ("exists", "specific", "original_vrf", "selected_vrf", "expected"),
        [
            (True, True, None, None, "matched"),
            (True, False, None, 8, "update"),
            (False, False, 8, 8, "matched"),
            (False, False, 8, 9, "sync"),
            (False, False, None, None, "sync"),
        ],
    )
    def test_status_matrix(self, exists, specific, original_vrf, selected_vrf, expected):
        status = self._view()._determine_status(exists, specific, original_vrf, selected_vrf)

        assert status == expected


class TestIPAddressVerifyEndpoint:
    """Verify request validation and rendered status through the real endpoint."""

    def test_invalid_json_and_non_object_json_return_specific_400_responses(self):
        from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

        malformed = view_post(SingleIPAddressVerifyView(), _request("{"))
        non_object = view_post(SingleIPAddressVerifyView(), _request("[1, 2]"))

        assert malformed.status_code == 400
        assert _json(malformed)["message"] == "Invalid JSON payload"
        assert non_object.status_code == 400
        assert _json(non_object)["message"] == "JSON payload must be an object"

    @pytest.mark.parametrize(
        ("changes", "message"),
        [
            ({"ip_address": None}, "No IP address provided"),
            ({"device_id": True}, "Invalid object ID"),
            ({"device_id": 1.5}, "Invalid object ID"),
            ({"device_id": "invalid"}, "Invalid object ID"),
            ({"vrf_id": True}, "Invalid VRF ID"),
            ({"vrf_id": 1.5}, "Invalid VRF ID"),
            ({"vrf_id": "invalid"}, "Invalid VRF ID"),
        ],
    )
    def test_invalid_fields_return_specific_400_responses(self, changes, message):
        payload = {
            "device_id": 2_147_483_647,
            "object_type": "device",
            "ip_address": "198.18.5.1/24",
            **changes,
        }

        response = _post_ip(payload)

        assert response.status_code == 400
        assert _json(response)["message"] == message

    def test_unknown_device_returns_404(self):
        response = _post_ip(
            {
                "device_id": 2_147_483_647,
                "object_type": "device",
                "ip_address": "198.18.6.1/24",
            }
        )

        assert response.status_code == 404
        assert _json(response)["message"] == "Object with ID 2147483647 not found"

    def test_cached_original_vrf_renders_synced_without_an_ip_row(self):
        from ipam.models import VRF

        from netbox_librenms_plugin.views.base.ip_addresses_view import SingleIPAddressVerifyView

        device = make_device("ip-cached-vrf")
        interface = make_interface(device, "Ethernet7")
        vrf = VRF.objects.create(name="cached-vrf", rd="64512:9")
        view = SingleIPAddressVerifyView()
        cache.set(
            view.get_cache_key(device, "ip_addresses", "default"),
            {
                "ip_addresses": [
                    {
                        "ip_with_mask": "198.18.7.1/24",
                        "vrf_id": vrf.pk,
                        "port_id": interface.pk,
                        "interface_name": interface.name,
                    }
                ]
            },
            timeout=300,
        )

        response = view_post(
            view,
            _request(
                {
                    "device_id": device.pk,
                    "object_type": "device",
                    "ip_address": "198.18.7.1/24",
                    "vrf_id": str(vrf.pk),
                    "server_key": "default",
                }
            ),
        )

        assert response.status_code == 200
        assert "Synced" in _json(response)["formatted_row"]["status"]

    def test_existing_address_in_selected_vrf_renders_synced(self):
        device = make_device("ip-existing")
        interface = make_interface(device, "Ethernet8")
        make_ip("198.18.8.1/24", assigned_object=interface)

        response = _post_ip(
            {
                "device_id": device.pk,
                "object_type": "device",
                "ip_address": "198.18.8.1/24",
            }
        )

        assert response.status_code == 200
        assert "Synced" in _json(response)["formatted_row"]["status"]

    def test_existing_address_in_a_different_vrf_renders_update(self):
        from ipam.models import VRF

        device = make_device("ip-update")
        interface = make_interface(device, "Ethernet9")
        make_ip("198.18.9.1/24", assigned_object=interface)
        vrf = VRF.objects.create(name="new-vrf", rd="64512:10")

        response = _post_ip(
            {
                "device_id": device.pk,
                "object_type": "device",
                "ip_address": "198.18.9.1/24",
                "vrf_id": vrf.pk,
            }
        )

        assert response.status_code == 200
        assert "Update" in _json(response)["formatted_row"]["status"]


class TestCableVerifyEndpoint:
    """Recompute cached cable rows from current NetBox interfaces and cables."""

    def test_missing_device_returns_the_empty_row(self):
        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        response = view_post(SingleCableVerifyView(), _request({"server_key": "default"}))

        assert response.status_code == 200
        assert _json(response)["formatted_row"]["cable_status"] == "Missing Ports"

    def test_malformed_snapshot_is_purged_and_returns_the_empty_row(self):
        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        device = make_device("cable-malformed")
        view = SingleCableVerifyView()
        server_key = view._render_server_key()
        assert server_key is not None
        key = view.get_cache_key(device, "links", server_key)
        cache.set(key, {"links": ["not-a-row"]}, timeout=300)

        response = view_post(
            view,
            _request({"device_id": device.pk, "row_id": "7", "server_key": server_key}),
        )

        assert response.status_code == 200
        assert _json(response)["formatted_row"]["cable_status"] == "Missing Ports"
        assert cache.get(key) is None

    def test_origin_must_match_the_selected_device(self):
        selected = make_device("cable-selected")
        origin = make_device("cable-other-origin")

        response = _post_cable(
            selected,
            [],
            row_id="1",
            origin_device_id=origin.pk,
        )

        assert response.status_code == 400
        assert _json(response)["message"] == "The cable page and selected device do not match."

    def test_serial_row_rejects_member_verification(self):
        device = make_device("cable-serial")
        row = {
            "_source": "serial",
            "local_port": "ttyS0",
            "local_port_id": "serial:7",
            "remote_device": "console-server",
        }

        response = _post_cable(device, [row], row_id="serial:7")

        assert response.status_code == 400
        assert _json(response)["message"] == "Serial cable rows have a fixed device owner."

    def test_real_interfaces_and_cable_render_current_links(self):
        local = make_device("cable-local")
        remote = make_device("cable-remote")
        local_interface = make_interface(local, "Ethernet10")
        remote_interface = make_interface(remote, "Ethernet11")
        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        server_key = SingleCableVerifyView()._render_server_key()
        assert server_key is not None
        _seed_librenms_id(local_interface, 1010, server_key)
        _seed_librenms_id(remote_interface, 1111, server_key)
        cable = cable_together(local_interface, remote_interface)
        row = {
            "local_port": "stale-local-name",
            "local_port_id": 1010,
            "remote_port": "stale-remote-name",
            "remote_port_id": 1111,
            "remote_device": remote.name,
            "remote_device_id": None,
            "netbox_local_interface_id": 999_999,
            "remote_device_url": "/stale/device/",
            "cable_status": "stale-status",
        }

        response = _post_cable(local, [row], row_id="1010")
        rendered = _json(response)["formatted_row"]

        assert response.status_code == 200
        assert f"/dcim/interfaces/{local_interface.pk}/" in rendered["local_port"]
        assert f"/dcim/interfaces/{remote_interface.pk}/" in rendered["remote_port"]
        assert f"/dcim/devices/{remote.pk}/" in rendered["remote_device"]
        assert f"/dcim/cables/{cable.pk}/" in rendered["cable_status"]
        assert "Cable Found" in rendered["cable_status"]
        assert "/stale/device/" not in rendered["remote_device"]
        assert "stale-status" not in rendered["cable_status"]

    def test_unresolved_oob_port_keeps_badge_and_never_renders_none(self):
        device = make_device("cable-oob")
        row = {
            "_source": "oob",
            "local_port": None,
            "local_port_id": 1212,
            "remote_port": "",
            "remote_device": "",
        }

        response = _post_cable(device, [row], row_id="1212")
        rendered = _json(response)["formatted_row"]

        assert response.status_code == 200
        assert rendered["cable_status"] == "Missing Interface"
        assert "From OOB controller" in rendered["local_port"]
        assert "None" not in rendered["local_port"]
