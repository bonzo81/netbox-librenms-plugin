"""
Regression tests for SingleCableVerifyView.post().

Fully DB-backed: the verify path re-enriches purely from live NetBox state
(get_device_by_id_or_name, enrich_remote_port and check_cable_status are ORM lookups — the
LibreNMS client is never touched once the links cache is warm), so these exercise real
Device/Interface/Cable objects, a real links cache entry and a real object-scoped request end to
end. The only patched boundary is get_available_servers() — i.e. which LibreNMS servers are
configured — so the POSTed server_key resolves deterministically.

Covers:
- Stale derived fields (ids/URLs from a previous enrichment) in the cached link are stripped and
  recomputed from current NetBox state.
- LibreNMS-sourced labels (local_port, remote_device) are HTML-escaped to prevent XSS.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache as real_cache
from django.test import RequestFactory

SERVER_KEY = "default"


def _cable_device(tag, ifaces):
    """Create a real Device named *tag* with named interfaces.

    ``ifaces`` is a list of ``(name, librenms_port_id)`` — a non-None port id binds the interface's
    ``librenms_id`` custom field for that server so the view's ``_librenms_id_q`` lookup resolves it.
    Returns ``(device, {name: interface})``.
    """
    from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site

    mfr, _ = Manufacturer.objects.get_or_create(name=f"CblMfr-{tag}", slug=f"cblmfr-{tag}")
    dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model=f"CblDT-{tag}", slug=f"cbldt-{tag}")
    role, _ = DeviceRole.objects.get_or_create(name="CblRole", slug="cblrole")
    site, _ = Site.objects.get_or_create(name="CblSite", slug="cblsite")
    device = Device.objects.create(name=tag, device_type=dt, role=role, site=site, status="active")
    made = {}
    for name, port_id in ifaces:
        iface = Interface.objects.create(device=device, name=name, type="1000base-t")
        if port_id is not None:
            iface.custom_field_data = {"librenms_id": {SERVER_KEY: port_id}}
            iface.save()
        made[name] = iface
    return device, made


def _cable_view_and_request(tag, body):
    """A real SingleCableVerifyView plus a real superuser request (so the perm gate + restrict resolve)."""
    from django.contrib.auth import get_user_model

    from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

    view = SingleCableVerifyView()
    request = RequestFactory().post("/verify-cable/", data=json.dumps(body), content_type="application/json")
    request.user = get_user_model().objects.create_superuser(username=f"cbl-{tag}", email="", password="x")
    view.request = request
    view.kwargs = {}
    view.args = ()
    return view, request


def _post_with_links(view, request, device, links):
    """Warm the real links cache for *device*, run post() (with 'default' configured), clean up, return JSON."""
    key = view.get_cache_key(device, "links", SERVER_KEY)
    real_cache.set(key, {"links": links})
    try:
        with patch(
            "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
            return_value={SERVER_KEY: "Default"},
        ):
            response = view.post(request)
    finally:
        real_cache.delete(key)
    return json.loads(response.content)


@pytest.fixture(autouse=True)
def _clear_cache_around_each_test():
    """Clear the real cache before and after each test so cache.set() link/port payloads (keyed by device PK, NOT rolled back with the test DB) can't feed a reused PK stale data."""
    from django.core.cache import cache
    from netbox_librenms_plugin.tests.conftest import clear_test_cache

    clear_test_cache(cache)
    yield
    clear_test_cache(cache)


@pytest.mark.django_db
class TestStaleFieldStripping:
    """Cached derived fields (URLs/ids from a previous enrichment) must be recomputed from live state."""

    def test_stale_remote_fields_recomputed_from_live_netbox(self):
        """A stale remote_device_url/id in cache is dropped; the row links the REAL remote device + cable."""
        from dcim.models import Cable

        local, lifaces = _cable_device("stale-local", [("eth0", 100)])
        remote, rifaces = _cable_device("switch-remote", [("eth1", 200)])
        cable = Cable(a_terminations=[lifaces["eth0"]], b_terminations=[rifaces["eth1"]])
        cable.save()

        view, request = _cable_view_and_request(
            "stale", {"device_id": local.pk, "local_port_id": 100, "server_key": SERVER_KEY}
        )
        content = _post_with_links(
            view,
            request,
            local,
            [
                {
                    "local_port": "eth0",
                    "local_port_id": 100,
                    "remote_port": "eth1",
                    "remote_port_id": 200,
                    "remote_device": "switch-remote",
                    "remote_device_id": None,
                    # Stale derived fields from a previous enrichment (a different remote object then):
                    "netbox_remote_device_id": 999,
                    "remote_device_url": "/dcim/devices/999/",
                    "netbox_remote_interface_id": 888,
                    "remote_port_url": "/dcim/interfaces/888/",
                    "cable_status": "No Cable",
                    "can_create_cable": True,
                }
            ],
        )

        row = content["formatted_row"]
        # Remote side recomputed to the REAL device, not the stale 999.
        assert f"/dcim/devices/{remote.pk}/" in row["remote_device"]
        assert "/dcim/devices/999/" not in row["remote_device"]
        # The real cable is found, so the stale "No Cable" is replaced.
        assert "Cable Found" in row["cable_status"]

    def test_stale_local_and_status_fields_recomputed(self):
        """Stale local interface id / cable_status in cache are stripped; the row uses live values."""
        local, lifaces = _cable_device("stale2-local", [("eth0", 100)])

        view, request = _cable_view_and_request(
            "stale2", {"device_id": local.pk, "local_port_id": 100, "server_key": SERVER_KEY}
        )
        content = _post_with_links(
            view,
            request,
            local,
            [
                {
                    "local_port": "eth0",
                    "local_port_id": 100,
                    "remote_port": "eth1",
                    "remote_port_id": 200,
                    "remote_device": "",  # no remote side → no remote enrichment / cable check
                    "remote_device_id": None,
                    # Stale derived fields that must not survive the strip-to-raw-keys step:
                    "netbox_local_interface_id": 999,
                    "local_port_url": "/stale/",
                    "cable_status": "stale-status",
                    "can_create_cable": True,
                }
            ],
        )

        row = content["formatted_row"]
        # Local port link recomputed to the REAL interface, not the stale id/url.
        assert f"/dcim/interfaces/{lifaces['eth0'].pk}/" in row["local_port"]
        assert "/stale/" not in row["local_port"]
        # cable_status was stripped (not a raw key) → falls back to the default, never the stale value.
        assert "stale-status" not in row["cable_status"]
        assert row["cable_status"] == "Missing Ports"


@pytest.mark.django_db
class TestXSSEscaping:
    """LibreNMS-sourced labels must be HTML-escaped in cable verify output (real render, real objects)."""

    def test_xss_in_local_port_name_escaped(self):
        """A malicious local_port name must be escaped in the HTML output."""
        local, lifaces = _cable_device("xss-local", [("eth0", 100)])
        xss = '<script>alert("xss")</script>'

        view, request = _cable_view_and_request(
            "xsslocal", {"device_id": local.pk, "local_port_id": 100, "server_key": SERVER_KEY}
        )
        content = _post_with_links(
            view,
            request,
            local,
            [
                {
                    "local_port": xss,  # interface still resolves by librenms_id below
                    "local_port_id": 100,
                    "remote_port": "eth1",
                    "remote_port_id": 200,
                    "remote_device": "",
                    "remote_device_id": None,
                }
            ],
        )

        local_port_html = content["formatted_row"]["local_port"]
        assert "<script>" not in local_port_html
        assert "&lt;script&gt;" in local_port_html

    def test_xss_in_remote_device_name_escaped(self):
        """A malicious remote_device name (unresolvable in NetBox) must be escaped in the HTML output."""
        local, lifaces = _cable_device("xss-remote", [("eth0", 100)])
        xss_device = "<img src=x onerror=alert(1)>"

        view, request = _cable_view_and_request(
            "xssremote", {"device_id": local.pk, "local_port_id": 100, "server_key": SERVER_KEY}
        )
        content = _post_with_links(
            view,
            request,
            local,
            [
                {
                    "local_port": "eth0",
                    "local_port_id": 100,
                    "remote_port": "eth1",
                    "remote_port_id": 200,
                    "remote_device": xss_device,  # no NetBox device by this name → escaped as text
                    "remote_device_id": None,
                }
            ],
        )

        remote_device_html = content["formatted_row"]["remote_device"]
        assert "<img " not in remote_device_html
        assert "&lt;img" in remote_device_html


def _cable_view_with_api(tag, body, *, api_server_key="default"):
    """A real SingleCableVerifyView + real superuser request, with _librenms_api stubbed only to supply
    the active-server fallback key (its constructor would otherwise need real LibreNMS config)."""
    from django.contrib.auth import get_user_model

    from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

    view = SingleCableVerifyView()
    view._librenms_api = MagicMock()
    view._librenms_api.server_key = api_server_key
    request = RequestFactory().post("/verify-cable/", data=json.dumps(body), content_type="application/json")
    request.user = get_user_model().objects.create_superuser(username=f"guard-{tag}", email="", password="x")
    view.request = request
    view.kwargs = {}
    view.args = ()
    return view, request


@pytest.mark.django_db
class TestServerKeyGuard:
    """A forged non-string server_key must not crash the membership check; a configured string is honoured."""

    @pytest.mark.parametrize("forged", [["prod"], {"prod": 1}])
    def test_unhashable_server_key_falls_back_without_crashing(self, forged):
        """A JSON array/object server_key must fall back (the isinstance guard) — no unhashable TypeError."""
        # No device_id → post() resolves server_key (the guarded line) then returns the empty row.
        view, request = _cable_view_with_api("forged", {"server_key": forged})
        response = view.post(request)  # must not raise an unhashable-type TypeError
        assert response.status_code == 200

    def test_valid_string_server_key_is_honoured_in_cache_namespace(self):
        """A configured string key scopes the REAL links cache namespace, via the real classmethod check (#108/#109).

        Unlike the old device_id="" version, this actually exercises the resolved server_key: it drives
        the real LibreNMSAPI.get_available_servers() classmethod (patched to configure 'prod') and asserts
        the resolved key threads into get_cache_key — so it fails if the membership check ever regresses.
        """
        device, _ = _cable_device("guard-valid", [("eth0", 100)])
        view, request = _cable_view_with_api(
            "valid", {"device_id": device.pk, "local_port_id": 100, "server_key": "prod"}
        )

        captured = {}
        real_get_cache_key = view.get_cache_key

        def spy(dev, kind, sk):
            captured["server_key"] = sk
            return real_get_cache_key(dev, kind, sk)

        with (
            patch(
                "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
                return_value={"prod": "Prod"},
            ),
            patch.object(view, "get_cache_key", side_effect=spy),
        ):
            view.post(request)

        assert captured["server_key"] == "prod"  # the posted, CONFIGURED key was honoured, not the active default


@pytest.mark.django_db
class TestCablesGetRenderDegradesOnBrokenDefaultServer:
    """A cached cables GET must degrade cleanly when the default LibreNMS server is misconfigured, not 500 on the lazy LibreNMSAPI() construction."""

    def test_cached_render_does_not_500_when_default_server_build_fails(self):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        from django.test import RequestFactory

        from netbox_librenms_plugin.views.object_sync.devices import DeviceCableTableView

        tag = "cables-broken-srv"
        mfr, _ = Manufacturer.objects.get_or_create(name=f"Mfr-{tag}", slug=f"mfr-{tag}")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model=f"DT-{tag}", slug=f"dt-{tag}")
        role, _ = DeviceRole.objects.get_or_create(name=f"Role-{tag}", slug=f"role-{tag}")
        site, _ = Site.objects.get_or_create(name=f"Site-{tag}", slug=f"site-{tag}")
        device = Device.objects.create(name=f"host-{tag}", device_type=dt, role=role, site=site, status="active")
        view = DeviceCableTableView()
        request = RequestFactory().get("/")
        User = get_user_model()
        request.user = User.objects.filter(is_superuser=True, is_active=True).first() or User.objects.create(
            username="cables-su", is_superuser=True, is_active=True
        )
        view.request = request

        # Seed the links cache under the DEGRADED (None) server_key — what the broken-server render reads.
        key = view.get_cache_key(device, "links", None)
        cache.set(
            key,
            {"links": [{"local_port": "Gi0/1", "local_port_id": "1", "remote_device": None}]},
            timeout=300,
        )

        def broken_cfg(_plugin, cfg_key, *args, **kwargs):
            # A non-empty 'servers' with no 'default' and no valid dict entry makes LibreNMSAPI()
            # raise ValueError in its constructor — the exact broken-default-server condition.
            return {"alpha": "not-a-dict"} if cfg_key == "servers" else None

        try:
            with patch("netbox_librenms_plugin.librenms_api.get_plugin_config", side_effect=broken_cfg):
                context = view.get_context_data(request, device)
            # Degrades: the cached links still render (server_key falls back to None) instead of a 500.
            assert context is not None
            assert context["table"] is not None
            assert context["server_key"] is None
        finally:
            cache.delete(key)


@pytest.mark.django_db
class TestSingleCableVerifyMisconfiguredDefault:
    """SingleCableVerifyView resolves configured servers via the classmethod, not the raising instance property."""

    def test_configured_key_does_not_500_when_default_broken(self):
        """A configured 'prod' key resolves without constructing the broken default client (issue #108/#109)."""
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
        from django.test import RequestFactory, override_settings

        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        mfr, _ = Manufacturer.objects.get_or_create(name="Mfr-scv", slug="mfr-scv")
        dt, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="DT-scv", slug="dt-scv")
        role, _ = DeviceRole.objects.get_or_create(name="Role-scv", slug="role-scv")
        site, _ = Site.objects.get_or_create(name="Site-scv", slug="site-scv")
        device = Device.objects.create(name="host-scv", device_type=dt, role=role, site=site, status="active")

        view = object.__new__(SingleCableVerifyView)
        view._librenms_api = None  # live property: accessing it would build LibreNMSAPI() (default)
        view.require_object_permissions_json = MagicMock(return_value=None)

        # DEFAULT present-but-broken (no url/token) → LibreNMSAPI() raises; get_available_servers()={'prod'}.
        cfg = {
            "netbox_librenms_plugin": {
                "servers": {
                    "prod": {"librenms_url": "http://prod.example", "api_token": "tok"},
                    "default": {},
                }
            }
        }
        request = RequestFactory().post(
            "/verify/",
            data=json.dumps({"device_id": device.pk, "local_port_id": "1", "server_key": "prod"}),
            content_type="application/json",
        )
        # RequestFactory skips AuthenticationMiddleware; the object-scoped device lookup reads
        # request.user, so attach a real superuser (this test is about server-key degradation, not
        # object scope).
        from django.contrib.auth import get_user_model

        request.user = get_user_model().objects.create_user(username="scv-user", password="x", is_superuser=True)
        view.request = request  # dispatch() normally wires this; the object-scoped lookup reads self.request.user
        with override_settings(PLUGINS_CONFIG=cfg):
            response = view.post(request)

        assert response.status_code == 200
        assert json.loads(response.content)["status"] == "success"


def _make_view(server_key="default"):
    """Create a SingleCableVerifyView instance without database access."""
    from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

    view = object.__new__(SingleCableVerifyView)
    view._librenms_api = MagicMock()
    view._librenms_api.server_key = server_key
    view.request = MagicMock()
    return view


def _make_request(body_dict):
    """Create a mock POST request with JSON body."""
    request = MagicMock()
    request.method = "POST"
    request.body = json.dumps(body_dict).encode()
    request.META = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
    return request


@pytest.mark.django_db
class TestVerifyDualNameFallback:
    """Issue #88 (verify parity): SingleCableVerifyView.post must resolve a row whose NetBox interface is named from the LibreNMS field the user is NOT currently displaying — using the same dual-name (local_port / local_port_alt) fallback as enrich_local_port."""

    def test_verify_resolves_interface_by_alternate_name(self):
        from django.core.cache import cache

        from dcim.models import Interface
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("verify-sw")
        # The NetBox interface is named from the ALTERNATE LibreNMS field (e.g. ifDescr),
        # which differs from the displayed local_port (ifName).
        iface = Interface.objects.create(device=device, name="GigabitEthernet0/1", type="1000base-t")

        view = _make_view()
        # Seed the links cache exactly as the GET path would. local_port_id (555) matches no
        # interface librenms_id, so resolution must fall back to the name — and only the
        # alternate name matches.
        link = {
            "local_port": "Gi0/1",  # displayed name — does NOT match iface.name
            "local_port_alt": "GigabitEthernet0/1",  # alternate field — matches iface.name
            "local_port_id": 555,
            "remote_device": "",
            "remote_port": "",
            "remote_port_id": None,
            "remote_device_id": None,
        }
        cache.set(view.get_cache_key(device, "links", "default"), {"links": [link]}, 300)

        request = _make_request({"device_id": device.pk, "local_port_id": 555, "server_key": "default"})
        response = view.post(request)
        payload = json.loads(response.content)

        # Resolved via the alternate name → local_port rendered as a link to the interface.
        local_port_html = payload["formatted_row"]["local_port"]
        assert f"/interfaces/{iface.pk}/" in local_port_html

    def test_verify_unresolved_none_local_port_does_not_render_literal_none(self):
        """A row whose local_port is None and resolves to no interface must render "" (or a badge), not the literal "None"."""
        from django.core.cache import cache

        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("verify-none-sw")
        view = _make_view()
        # OOB row whose local interface name couldn't be resolved: local_port is None while
        # local_port_id is set (matches no NetBox interface → falls to the unmatched branch).
        link = {
            "local_port": None,
            "local_port_alt": None,
            "local_port_id": 7777,
            "remote_device": "",
            "remote_port": "",
            "remote_port_id": None,
            "remote_device_id": None,
            "_source": "oob",
        }
        cache.set(view.get_cache_key(device, "links", "default"), {"links": [link]}, 300)

        request = _make_request({"device_id": device.pk, "local_port_id": 7777, "server_key": "default"})
        response = view.post(request)
        payload = json.loads(response.content)

        # Prove the intended branch actually ran. The default empty row (post() fallback) carries
        # cable_status "Missing Ports" and local_port "", so the None-checks below would pass
        # vacuously on it. The matched-link-but-unresolved-interface branch instead reports
        # "Missing Interface" and re-applies the OOB badge — pin both so a regression that drops to
        # the empty row (or stops processing the OOB row) fails here.
        assert "formatted_row" in payload
        assert "local_port" in payload["formatted_row"]
        assert payload["formatted_row"]["cable_status"] == "Missing Interface"
        assert "From OOB controller" in payload["formatted_row"]["local_port"]
        # render_local_port in the table was fixed to normalize None→""; the verify path must match.
        assert payload["formatted_row"]["local_port"] != "None"
        assert "None" not in payload["formatted_row"]["local_port"]


@pytest.mark.django_db
class TestRemoteDeviceResolutionExcludesOOB:
    """A cable's remote device_id is the remote device's OWN LibreNMS identity, so resolving it must not also match a different device that merely references that id as its OOB controller — that over-match raises MultipleObjectsReturned and blocks the (valid) resolution."""

    def test_remote_device_id_does_not_match_an_oob_controller_reference(self):
        from netbox_librenms_plugin.tests.conftest import make_device
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        host = make_device("remote-host", librenms_cf={"default": {"id": 42}})
        # A DIFFERENT device references LibreNMS id 42 as its OOB controller (not its own id).
        make_device("oob-referencer", librenms_cf={"default": {"oob": {"id": 42}}})

        view = object.__new__(BaseCableTableView)
        # Pass a hostname that matches NO device so the resolution can only succeed via the id-42
        # path: with "remote-host" as the hint, get_device_by_id_or_name() could still return host
        # through the hostname fallback even if the id lookup regressed to MultipleObjectsReturned,
        # making the test pass vacuously.
        device, found, error = view.get_device_by_id_or_name(42, "no-matching-hostname", "default")

        # Resolves to the device whose own LibreNMS id is 42, not MultipleObjectsReturned.
        assert found is True
        assert error is None
        assert device.pk == host.pk


@pytest.mark.django_db
class TestOOBRowsNeverActionable:
    """check_cable_status must keep context-only OOB rows non-actionable (no Sync Cable)."""

    # OOB-controller rows are merged into the host's cable list for context only (shared-LOM
    # detection) and are skipped by SyncCablesView.process_single_interface, so they must never
    # offer a Sync Cable action. check_cable_status is the single source of truth for
    # can_create_cable (read by both the table render and the verify response), so it stays
    # non-actionable even when an OOB row's shared-name local port resolves to a host interface.

    def _make_resolved_no_cable_link(self, source):
        """A link whose both endpoints resolve to real, cable-free NetBox interfaces."""
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        local_if = make_interface(make_device(f"local-{source}"), "eth0")
        remote_if = make_interface(make_device(f"remote-{source}"), "eth1")
        return {
            "netbox_local_interface_id": local_if.pk,
            "netbox_remote_interface_id": remote_if.pk,
            "_source": source,
        }

    def test_host_row_with_no_cable_is_actionable(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        link = view.check_cable_status(self._make_resolved_no_cable_link("main"))

        assert link["cable_status"] == "No Cable"
        assert link["can_create_cable"] is True

    def test_oob_row_with_no_cable_is_not_actionable(self):
        from netbox_librenms_plugin.views.base.cables_view import BaseCableTableView

        view = object.__new__(BaseCableTableView)
        link = view.check_cable_status(self._make_resolved_no_cable_link("oob"))

        # Status is still reported, but no Sync Cable action is offered.
        assert link["cable_status"] == "No Cable"
        assert link["can_create_cable"] is False

    @pytest.mark.parametrize(
        "source,expect_sync",
        [("main", True), ("oob", False)],
    )
    def test_verify_response_offers_sync_only_for_non_oob_rows(self, source, expect_sync):
        """Verify POST offers Sync Cable for a host row but not for an OOB row."""
        # Both rows resolve to cable-free interfaces on both ends; only _source differs.
        from django.core.cache import cache

        from dcim.models import Interface
        from netbox_librenms_plugin.tests.conftest import make_device

        local_dev = make_device(f"verify-local-{source}")
        Interface.objects.create(device=local_dev, name="eth0", type="1000base-t")
        remote_dev = make_device(f"verify-remote-{source}")
        Interface.objects.create(device=remote_dev, name="eth9", type="1000base-t")

        view = _make_view()
        link = {
            "local_port": "eth0",
            "local_port_id": 700,
            "remote_port": "eth9",
            "remote_device": remote_dev.name,
            "remote_port_id": None,
            "remote_device_id": None,
            "_source": source,
        }
        cache.set(view.get_cache_key(local_dev, "links", "default"), {"links": [link]}, 300)

        request = _make_request({"device_id": local_dev.pk, "local_port_id": 700, "server_key": "default"})
        payload = json.loads(view.post(request).content)
        actions = payload["formatted_row"]["actions"]

        assert ("Sync Cable" in actions) is expect_sync

    def test_oob_row_never_links_a_host_interface(self):
        """Verify must not resolve an OOB row's local end against the HOST device.

        The controller-managed port lives on the OOB device; a shared name (or colliding
        stored librenms_id) would render a clickable HOST-interface link on it — mirrors
        the enrich_local_port guard on the initial table render.
        """
        from django.core.cache import cache

        from dcim.models import Interface
        from netbox_librenms_plugin.tests.conftest import make_device

        local_dev = make_device("verify-oob-collide")
        Interface.objects.create(device=local_dev, name="eth0", type="1000base-t")  # host iface, same name

        view = _make_view()
        link = {"local_port": "eth0", "local_port_id": 700, "remote_device": "", "_source": "oob"}
        cache.set(view.get_cache_key(local_dev, "links", "default"), {"links": [link]}, 300)

        request = _make_request({"device_id": local_dev.pk, "local_port_id": 700, "server_key": "default"})
        row = json.loads(view.post(request).content)["formatted_row"]

        assert "/dcim/interfaces/" not in row["local_port"]  # no host-interface link
        assert "eth0" in row["local_port"]  # the label (with its OOB badge) still renders
        assert "OOB" in row["local_port"]  # the badge marks the row as controller-sourced


@pytest.mark.django_db
class TestSingleCableVerifyServerKeyRouting:
    """post() must resolve the links cache under the POSTed server_key, else the api's bound key."""

    def _seed(self, view, device, server_key):
        from django.core.cache import cache

        # A minimal host link keyed under *server_key*; local port matches the interface by name.
        link = {"local_port": "eth0", "local_port_id": 700, "remote_device": "", "_source": "main"}
        cache.set(view.get_cache_key(device, "links", server_key), {"links": [link]}, 300)

    def test_post_server_key_selects_that_servers_cache(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("verify-key-dev")
        Interface.objects.create(device=device, name="eth0", type="1000base-t")
        view = _make_view(server_key="default-server")
        self._seed(view, device, "production")  # links cached ONLY under 'production'

        request = _make_request({"device_id": device.pk, "local_port_id": 700, "server_key": "production"})
        with patch(
            "netbox_librenms_plugin.librenms_api.LibreNMSAPI.get_available_servers",
            return_value={"production": "Production"},
        ):
            row = json.loads(view.post(request).content)["formatted_row"]

        # The 'production' cache was read → the port link renders (not the empty Missing-Ports row).
        assert "eth0" in row["local_port"]
        assert "/dcim/interfaces/" in row["local_port"]

    def test_absent_server_key_falls_back_to_api_default(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("verify-fallback-dev")
        Interface.objects.create(device=device, name="eth0", type="1000base-t")
        view = _make_view(server_key="fallback-server")
        self._seed(view, device, "fallback-server")  # cached under the api's bound key

        request = _make_request({"device_id": device.pk, "local_port_id": 700})  # POST omits server_key
        row = json.loads(view.post(request).content)["formatted_row"]

        assert "eth0" in row["local_port"]  # the fallback (api.server_key) cache key was used

    def test_post_server_key_does_not_read_a_different_servers_cache(self):
        from netbox_librenms_plugin.tests.conftest import make_device

        device = make_device("verify-miss-dev")
        view = _make_view(server_key="default-server")
        self._seed(view, device, "staging")  # cached under 'staging' only

        request = _make_request({"device_id": device.pk, "local_port_id": 700, "server_key": "production"})
        row = json.loads(view.post(request).content)["formatted_row"]

        # No cross-server bleed: the 'production' lookup misses → the default Missing-Ports row.
        assert row["local_port"] == ""
        assert row["cable_status"] == "Missing Ports"


@pytest.mark.django_db
class TestEnrichRemotePortEmptyPort:
    """A resolvable remote device with no advertised remote port must not crash the Cables tab."""

    def _make_device(self, name):
        from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

        site, _ = Site.objects.get_or_create(name="erp-site", slug="erp-site")
        mfr, _ = Manufacturer.objects.get_or_create(name="erp-mfr", slug="erp-mfr")
        dtype, _ = DeviceType.objects.get_or_create(manufacturer=mfr, model="erp-dt", slug="erp-dt")
        role, _ = DeviceRole.objects.get_or_create(name="erp-role", slug="erp-role")
        return Device.objects.create(name=name, site=site, device_type=dtype, role=role)

    def _make_view(self):
        from netbox_librenms_plugin.views.base.cables_view import SingleCableVerifyView

        view = object.__new__(SingleCableVerifyView)
        view._librenms_api = MagicMock()
        view._librenms_api.server_key = "default"
        view.request = MagicMock()
        return view

    def test_enrich_links_data_survives_remote_device_without_port(self):
        """LLDP neighbor advertising a resolvable device but no remote port: enrich_links_data must not crash (regression: enrich_remote_port returned None → link.get() AttributeError)."""
        obj = self._make_device("erp-local-sw")
        remote = self._make_device("erp-remote-sw")
        view = self._make_view()

        # remote_device resolves by name; remote_port empty (no port advertised)
        links = [{"remote_device": "erp-remote-sw", "remote_port": "", "remote_port_id": None}]

        result = view.enrich_links_data(links, obj, server_key="default")  # must NOT raise AttributeError

        assert result[0]["netbox_remote_device_id"] == remote.pk
        assert result[0]["device_id"] == obj.id

    def test_enrich_remote_port_returns_link_when_remote_port_empty(self):
        """enrich_remote_port returns the link (never None) when remote_port is empty, so callers that reassign link don't NoneType-crash."""
        remote = self._make_device("erp-remote-sw2")
        view = self._make_view()

        link = {"remote_port": ""}
        result = view.enrich_remote_port(link, remote, server_key="default")

        assert result is link


@pytest.mark.django_db
class TestResolveLocalInterfaceCore:
    """The shared id→dual-name resolution core used by BOTH enrich_local_port and the verify
    re-resolution (extracted so the issue-#88 name fallback / precedence can't drift between
    the two copies again)."""

    def _dev(self, name):
        from netbox_librenms_plugin.tests.conftest import make_device, make_interface

        device = make_device(name)
        return device, make_interface(device, "eth-by-name")

    def test_librenms_id_beats_name(self):
        from dcim.models import Interface

        from netbox_librenms_plugin.views.base.cables_view import _resolve_local_interface

        device, by_name = self._dev("core-id-wins")
        by_id = Interface.objects.create(device=device, name="other-name", type="1000base-t")
        by_id.custom_field_data["librenms_id"] = {"default": 4242}
        by_id.save()

        got = _resolve_local_interface(device, "default", 4242, ["eth-by-name"])
        assert got == by_id  # the stable id match wins over the name candidate

    def test_name_fallback_covers_all_candidates(self):
        from netbox_librenms_plugin.views.base.cables_view import _resolve_local_interface

        device, by_name = self._dev("core-name-fb")
        # No id match anywhere: the ALTERNATE candidate (issue #88) must still resolve.
        got = _resolve_local_interface(device, "default", 9999, ["displayed-name", "eth-by-name"])
        assert got == by_name

    def test_empty_inputs_resolve_nothing(self):
        from netbox_librenms_plugin.views.base.cables_view import _resolve_local_interface

        device, _ = self._dev("core-empty")
        assert _resolve_local_interface(device, "default", None, []) is None
