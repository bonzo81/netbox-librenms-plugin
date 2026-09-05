"""
Resolution and refresh coverage for ``views/base/cables_view.py``.

The module's primary homes are ``test_cable_verify.py`` (SingleCableVerifyView),
``test_cable_resync.py`` and ``test_serial_cables_view.py``. This file covers the paths
those miss: the read-scoped querysets, the local/remote endpoint resolution fallbacks, the
OOB link merge failures and the POST refresh exits. Every test uses real NetBox rows, real
permission grants and the in-repo loopback LibreNMS, so the real ``LibreNMSAPI`` runs the
HTTP calls.
"""

import json

import pytest

from netbox_librenms_plugin.tests.conftest import (
    configure_librenms_servers,
    make_device,
    make_interface,
    make_ip,
    make_virtual_chassis,
)
from netbox_librenms_plugin.tests.view_test_helpers import (
    grant,
    make_request,
    make_superuser,
    make_user_with_perms,
    message_texts,
    post as _post,
)


pytestmark = pytest.mark.django_db

SERVER_KEY = "alpha"


def _bind_server(settings, server):
    """Point the plugin at the loopback LibreNMS and return a client bound to it."""
    from netbox_librenms_plugin.librenms_api import LibreNMSAPI

    configure_librenms_servers(settings, {SERVER_KEY: {"librenms_url": server.url, "api_token": "test-token"}})
    return LibreNMSAPI(server_key=SERVER_KEY)


def _map_device(device, librenms_id=None, *, oob=None):
    """Persist the device's LibreNMS mapping, optionally with an OOB controller sub-entry."""
    entry = {}
    if librenms_id is not None:
        entry["id"] = librenms_id
    if oob is not None:
        entry["oob"] = oob
    device.custom_field_data["librenms_id"] = {SERVER_KEY: entry}
    device.save(update_fields=["custom_field_data"])
    return device


def _map_interface(interface, port_id):
    """Bind a NetBox interface to a LibreNMS port id under the test server key."""
    interface.custom_field_data["librenms_id"] = {SERVER_KEY: port_id}
    interface.save(update_fields=["custom_field_data"])
    return interface


def _cable_view(settings, server, *, request=None, user=None):
    """Build a real DeviceCableTableView bound to the loopback client and a real request."""
    from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

    view = DeviceCableTableView()
    view._librenms_api = _bind_server(settings, server)
    view.setup(request if request is not None else make_request("get", user=user or make_superuser()))
    return view


def _links_route(server, device_id, links):
    """Register a LibreNMS device-links response."""
    server.register(f"/api/v0/devices/{device_id}/links", {"status": "ok", "links": links})


@pytest.mark.django_db
class TestCableViewIpAddress:
    def test_primary_ip_is_reported_and_a_device_without_one_reports_none(self):
        """get_ip_address returns the bare primary IP, and None when the device has none."""
        from dcim.models import Device

        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        with_ip = make_device("cable-ip-present")
        without_ip = make_device("cable-ip-absent")
        address = make_ip("10.43.0.9/24", assigned_object=make_interface(with_ip, "eth0"))
        with_ip.primary_ip4 = address
        with_ip.save()
        with_ip = Device.objects.get(pk=with_ip.pk)

        view = DeviceCableTableView()

        assert view.get_ip_address(with_ip) == "10.43.0.9"
        assert view.get_ip_address(without_ip) is None


@pytest.mark.django_db
class TestCableViewableQuerysetFallback:
    def test_an_anonymous_request_reads_the_unscoped_queryset(self):
        """Without an authenticated user there is no grant to scope by, so the plain manager is used."""
        from dcim.models import Device
        from django.contrib.auth.models import AnonymousUser

        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        granted = make_device("cable-scope-granted")
        hidden = make_device("cable-scope-hidden")
        scoped_user = make_user_with_perms("cable-scope-user", [("view", Device)], constraints={"pk": granted.pk})

        view = DeviceCableTableView()
        view.setup(make_request("get", user=scoped_user))
        visible = set(view._viewable_queryset(Device).values_list("pk", flat=True))
        assert granted.pk in visible
        assert hidden.pk not in visible

        anonymous_view = DeviceCableTableView()
        anonymous_request = make_request("get", user=scoped_user)
        anonymous_request.user = AnonymousUser()
        anonymous_view.setup(anonymous_request)
        unscoped_view = set(anonymous_view._viewable_queryset(Device).values_list("pk", flat=True))
        unscoped_change = set(anonymous_view._changeable_queryset(Device).values_list("pk", flat=True))

        assert {granted.pk, hidden.pk} <= unscoped_view
        assert {granted.pk, hidden.pk} <= unscoped_change


@pytest.mark.django_db
class TestCablePortsDataWithoutHostMapping:
    def test_no_host_librenms_id_returns_empty_ports_without_reading_the_cache(self, librenms_server, settings):
        """An OOB-only/unmapped device must not resurrect a stale host-ports snapshot or fetch."""
        from django.core.cache import cache

        device = make_device("cable-ports-unmapped")
        view = _cable_view(settings, librenms_server)
        ports_key = view.get_cache_key(device, "ports", SERVER_KEY)
        cache.set(ports_key, {"ports": [{"port_id": 7, "ifName": "stale0"}]}, timeout=300)

        ports_data = view.get_ports_data(device, server_key=SERVER_KEY)

        assert ports_data == {"ports": []}
        assert librenms_server.requests == []

    def test_a_failed_ports_fetch_returns_an_empty_payload(self, librenms_server, settings):
        """A LibreNMS ports failure must degrade to an empty payload, not the error string."""
        device = make_device("cable-ports-fetch-fail")
        view = _cable_view(settings, librenms_server)
        view.librenms_id = 141
        # /api/v0/devices/141/ports stays unregistered, so the loopback answers 404.

        ports_data = view.get_ports_data(device, server_key=SERVER_KEY)

        assert ports_data == {"ports": []}
        assert "/api/v0/devices/141/ports" in [r["path"] for r in librenms_server.requests]


@pytest.mark.django_db
class TestCableLinkCollection:
    def test_an_unmapped_local_port_id_falls_back_to_the_librenms_port_name(self, librenms_server, settings):
        """When the ports map has no entry for a link, the LibreNMS-reported local_port is used."""
        device = _map_device(make_device("cable-name-fallback"), 61)
        librenms_server.ports_response(device_id=61, ports=[{"port_id": 1, "ifName": "eth0"}])
        _links_route(
            librenms_server,
            61,
            [
                # local_port_id 999 is absent from the ports payload above.
                {"id": 1, "local_port_id": 999, "local_port": "eth9", "remote_hostname": "peer", "remote_port": "e1"}
            ],
        )
        view = _cable_view(settings, librenms_server)

        links = view.get_links_data(device, server_key=SERVER_KEY)

        assert [link["local_port"] for link in links] == ["eth9"]


@pytest.mark.django_db
class TestCableRemoteDeviceResolution:
    def test_a_non_numeric_remote_device_id_falls_back_to_the_hostname(self, librenms_server, settings):
        """A malformed remote_device_id must not raise; the name match still binds the device."""
        remote = make_device("cable-remote-by-name")
        view = _cable_view(settings, librenms_server)

        device, matched, error = view.get_device_by_id_or_name(
            "not-a-number", "cable-remote-by-name", server_key=SERVER_KEY
        )

        assert (device, matched, error) == (remote, True, None)

    def test_two_devices_sharing_a_librenms_id_report_the_ambiguity(self, librenms_server, settings):
        """A duplicated LibreNMS id must fail closed with the ambiguity message, not pick one."""
        _map_device(make_device("cable-dup-a"), 77)
        _map_device(make_device("cable-dup-b"), 77)
        view = _cable_view(settings, librenms_server)

        device, matched, error = view.get_device_by_id_or_name(77, "cable-dup-a", server_key=SERVER_KEY)

        assert device is None
        assert matched is False
        assert error == "Multiple devices found with the same LibreNMS ID: 77."


@pytest.mark.django_db
class TestCableLocalPortEnrichment:
    def test_a_plain_device_resolves_its_own_interface(self, librenms_server, settings):
        """Without a virtual chassis the local end resolves on the viewed device itself."""
        device = make_device("cable-local-plain")
        interface = make_interface(device, "eth0")
        view = _cable_view(settings, librenms_server)
        link = {"local_port": "eth0", "local_port_id": 500, "_source": "main"}

        view.enrich_local_port(link, device, server_key=SERVER_KEY)

        assert link["netbox_local_interface_id"] == interface.pk
        assert link["local_port_url"] == f"/dcim/interfaces/{interface.pk}/"

    def test_a_merged_oob_row_never_binds_a_host_interface(self, librenms_server, settings):
        """An OOB row's local port lives on the controller, so a same-named host interface must not bind."""
        device = make_device("cable-local-oob")
        make_interface(device, "eth0")
        view = _cable_view(settings, librenms_server)
        link = {"local_port": "eth0", "local_port_id": 502, "_source": "oob"}

        resolved = view.enrich_local_port(link, device, server_key=SERVER_KEY)

        assert resolved is None
        assert "netbox_local_interface_id" not in link
        assert "local_port_url" not in link

    def test_a_chassis_member_is_selected_from_the_port_name(self, librenms_server, settings):
        """On a VC the local end resolves on the member the port name names, not the viewed member."""
        member1 = make_device("cable-local-vc-1")
        member2 = make_device("cable-local-vc-2")
        make_virtual_chassis("cable-local-vc", member1, member2)
        make_interface(member1, "Ethernet1")
        owned = make_interface(member2, "Ethernet2")
        view = _cable_view(settings, librenms_server)
        link = {"local_port": "Ethernet2", "local_port_id": 501, "_source": "main"}

        view.enrich_local_port(link, member1, server_key=SERVER_KEY)

        assert link["netbox_local_interface_id"] == owned.pk
        assert link["netbox_local_device_id"] == member2.pk

    def test_a_serial_row_stays_unresolved_when_the_sync_owner_is_out_of_scope(self, librenms_server, settings):
        """A serial row must not bind a ConsoleServerPort on a chassis owner the request cannot view."""
        from dcim.models import ConsoleServerPort, Device

        viewed = make_device("cable-serial-viewed")
        owner = _map_device(make_device("cable-serial-owner"), 88)
        make_virtual_chassis("cable-serial-vc", viewed, owner)
        ConsoleServerPort.objects.create(device=owner, name="console1")
        # The viewed member carries a same-named port, so binding the viewed device instead of
        # the out-of-scope sync owner would silently resolve the wrong console port.
        ConsoleServerPort.objects.create(device=viewed, name="console1")
        scoped_user = make_user_with_perms("cable-serial-user", [("view", Device)], constraints={"pk": viewed.pk})
        scoped_user = grant(scoped_user, "view", ConsoleServerPort)
        view = _cable_view(settings, librenms_server, user=scoped_user)
        link = {"local_port": "console1", "_source": "serial"}

        resolved = view.enrich_local_port(link, viewed, server_key=SERVER_KEY)

        assert resolved is None
        assert "netbox_local_interface_id" not in link


@pytest.mark.django_db
class TestCableRemotePortEnrichment:
    def test_the_remote_end_resolves_its_own_server_key(self, librenms_server, settings):
        """enrich_remote_port with no server_key must resolve the active one, or the id match misses."""
        remote = make_device("cable-remote-key")
        # The NetBox name differs from the LibreNMS name, so only the port-id match can bind it.
        interface = _map_interface(make_interface(remote, "GigabitEthernet1/0/1"), 620)
        view = _cable_view(settings, librenms_server)
        link = {"remote_port": "Gi1/0/1", "remote_port_id": 620}

        view.enrich_remote_port(link, remote)

        assert link["netbox_remote_interface_id"] == interface.pk


@pytest.mark.django_db
class TestCableRefreshStaleServerKey:
    def test_unconfigured_posted_key_renders_the_error_partial(self, librenms_server, settings):
        """A POSTed key that no longer resolves errors and re-renders the partial under the bound key."""
        device = _map_device(make_device("cable-stale-key"), 91)
        view = _cable_view(settings, librenms_server)
        request = make_request("post", {"server_key": "ghost"}, user=make_superuser())

        response = _post(view, request, pk=device.pk)

        assert response.status_code == 200
        assert message_texts(request, "error") == ["Selected LibreNMS server is no longer configured."]
        assert view.active_server_key == SERVER_KEY
        assert librenms_server.requests == []


@pytest.mark.django_db
class TestCableRefreshFetchFailure:
    def test_a_failed_links_fetch_drops_the_previous_snapshot(self, librenms_server, settings):
        """A refresh that cannot fetch must purge the stale links cache instead of serving it."""
        from django.core.cache import cache

        device = _map_device(make_device("cable-fetch-fail"), 101)
        librenms_server.ports_response(device_id=101, ports=[{"port_id": 1, "ifName": "eth0"}])
        # /api/v0/devices/101/links stays unregistered, so the loopback answers 404.
        view = _cable_view(settings, librenms_server)
        links_key = view.get_cache_key(device, "links", SERVER_KEY)
        cache.set(links_key, {"links": [{"local_port": "eth0", "local_port_id": 1}]}, timeout=300)
        request = make_request("post", {"server_key": SERVER_KEY}, user=make_superuser())

        response = _post(view, request, pk=device.pk)

        assert response.status_code == 200
        assert cache.get(links_key) is None
        assert any(text.startswith("Failed to fetch links from LibreNMS:") for text in message_texts(request, "error"))


@pytest.mark.django_db
class TestCableRefreshOobMerge:
    def test_a_corrupt_oob_id_warns_instead_of_dropping_the_rows_silently(self, librenms_server, settings):
        """A linked OOB controller with an uncoercible id must warn, not be skipped in silence."""
        device = _map_device(make_device("cable-oob-corrupt"), 111, oob={"id": "not-an-id", "type": "idrac"})
        librenms_server.ports_response(device_id=111, ports=[{"port_id": 1, "ifName": "eth0"}])
        _links_route(
            librenms_server,
            111,
            [{"id": 1, "local_port_id": 1, "local_port": "eth0", "remote_hostname": "peer", "remote_port": "e1"}],
        )
        view = _cable_view(settings, librenms_server)
        request = make_request("post", {"server_key": SERVER_KEY}, user=make_superuser())

        _post(view, request, pk=device.pk)

        assert message_texts(request, "warning") == [
            "Cables refreshed, but OOB controller links fetch failed; "
            "showing host cables only. See server logs for details."
        ]
        assert view._oob_links_fetch_failed is True
        # The corrupt id must never become a request path.
        assert not [r["path"] for r in librenms_server.requests if "not-an-id" in r["path"]]

    def test_a_failed_oob_links_fetch_warns_and_keeps_the_host_rows(self, librenms_server, settings):
        """A 404 on the OOB controller's links endpoint warns and leaves the host rows in place."""
        device = _map_device(make_device("cable-oob-fetch-fail"), 121, oob={"id": 122, "type": "idrac"})
        librenms_server.ports_response(device_id=121, ports=[{"port_id": 1, "ifName": "eth0"}])
        _links_route(
            librenms_server,
            121,
            [{"id": 1, "local_port_id": 1, "local_port": "eth0", "remote_hostname": "peer", "remote_port": "e1"}],
        )
        # /api/v0/devices/122/links stays unregistered, so the loopback answers 404.
        view = _cable_view(settings, librenms_server)
        request = make_request("post", {"server_key": SERVER_KEY}, user=make_superuser())

        _post(view, request, pk=device.pk)

        assert message_texts(request, "warning") == [
            "Cables refreshed, but OOB controller links fetch failed; "
            "showing host cables only. See server logs for details."
        ]
        assert view._oob_links_fetch_failed is True
        assert "/api/v0/devices/122/links" in [r["path"] for r in librenms_server.requests]

    def test_a_malformed_oob_links_payload_warns_and_keeps_the_host_rows(self, librenms_server, settings):
        """A 200 whose OOB "links" is not a list must warn instead of crashing on iteration."""
        device = _map_device(make_device("cable-oob-malformed"), 151, oob={"id": 152, "type": "idrac"})
        librenms_server.ports_response(device_id=151, ports=[{"port_id": 1, "ifName": "eth0"}])
        _links_route(
            librenms_server,
            151,
            [{"id": 1, "local_port_id": 1, "local_port": "eth0", "remote_hostname": "peer", "remote_port": "e1"}],
        )
        librenms_server.register("/api/v0/devices/152/links", {"status": "ok", "links": {"not": "a list"}})
        view = _cable_view(settings, librenms_server)
        request = make_request("post", {"server_key": SERVER_KEY}, user=make_superuser())

        _post(view, request, pk=device.pk)

        assert message_texts(request, "warning") == [
            "Cables refreshed, but OOB controller links fetch failed; "
            "showing host cables only. See server logs for details."
        ]
        assert view._oob_links_fetch_failed is True


@pytest.mark.django_db
class TestSingleCableVerifyPermissions:
    def test_a_user_without_view_device_is_refused_before_the_device_is_resolved(self):
        """The verify endpoint gates on dcim.view_device and answers 403 JSON."""
        from dcim.models import Interface

        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        device = make_device("cable-verify-denied")
        user = make_user_with_perms("cable-verify-user", [("view", Interface)])
        request = make_request(
            "post",
            json.dumps({"device_id": device.pk, "row_id": "1"}),
            user=user,
            content_type="application/json",
        )
        view = SingleCableVerifyView()
        view.setup(request)

        response = view.post(request)

        assert response.status_code == 403
        assert json.loads(response.content)["error"] == "Missing permissions: dcim.view_device"


@pytest.mark.django_db
class TestSingleCableVerifyRemoteDeviceMissing:
    def test_an_unknown_remote_hostname_is_reported_as_not_found(self, librenms_server, settings):
        """A remote hostname with no NetBox device must render the not-found status, not a link."""
        from django.core.cache import cache

        device = _map_device(make_device("cable-verify-remote-missing"), 131)
        view = _cable_view(settings, librenms_server)
        links_key = view.get_cache_key(device, "links", SERVER_KEY)
        cache.set(
            links_key,
            {
                "links": [
                    {
                        # No NetBox interface carries this name, so the local end stays unresolved.
                        "local_port": "eth-absent",
                        "local_port_id": 700,
                        "remote_port": "Gi0/1",
                        "remote_device": "not-in-netbox",
                        "remote_device_id": None,
                        "_source": "main",
                    }
                ]
            },
            timeout=300,
        )
        request = make_request(
            "post",
            json.dumps({"device_id": device.pk, "row_id": "700", "server_key": SERVER_KEY}),
            user=make_superuser(),
            content_type="application/json",
        )
        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        verify = SingleCableVerifyView()
        verify._librenms_api = _bind_server(settings, librenms_server)
        verify.setup(request)

        response = verify.post(request)

        row = json.loads(response.content)["formatted_row"]
        assert row["cable_status"] == "Device Not Found in NetBox"
        assert row["remote_device"] == "not-in-netbox"
