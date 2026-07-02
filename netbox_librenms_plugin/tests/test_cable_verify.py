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
from unittest.mock import patch

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
