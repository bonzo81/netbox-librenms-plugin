"""Render tests for the CaptureDataShapeView modal (capture → anonymize → novelty → issue link)."""

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from netbox_librenms_plugin.tests.conftest import make_device
from netbox_librenms_plugin.tests.recordings import load_recording
from netbox_librenms_plugin.views.data_shapes import CaptureDataShapeView


def test_capture_data_shape_url_reverses():
    """The capture URL resolves with a device_id kwarg (so the sync-page button's {% url %} works)."""
    from django.urls import reverse

    url = reverse("plugins:netbox_librenms_plugin:capture_data_shape", kwargs={"device_id": 7})
    assert url.endswith("/device/7/capture-data-shape/")


def test_capture_button_reads_server_key_from_url_in_shipped_template():
    """The sync-page capture button must read server_key from the page URL (js hx-vals), like the interface-sync controls, since the base context has no top-level server_key — a {% if server_key %} URL guard would be dead and capture would always hit the default server."""
    from pathlib import Path

    import netbox_librenms_plugin

    source = (
        Path(netbox_librenms_plugin.__file__).parent
        / "templates"
        / "netbox_librenms_plugin"
        / "librenms_sync_base.html"
    ).read_text()
    # Scope to the capture button block (assert on the SHIPPED source so the test fails if the wiring
    # regresses, rather than a hand-copied snippet that can drift — mirrors TestMigratedTransferIpDeviceOnlyGate).
    idx = source.find("capture_data_shape")
    assert idx != -1, "capture button missing from librenms_sync_base.html"
    block = source[idx : idx + 600]
    # Reads server_key from the URL at request time (the interface-sync sibling pattern)...
    assert "URLSearchParams(window.location.search).get('server_key')" in block
    # ...and does NOT depend on a top-level {% if server_key %} the base context never sets.
    assert "{% if server_key %}" not in block
    # When the URL omits server_key, it falls back to the active page server (NOT an empty string,
    # which would silently capture against the default server) — mirroring the interface-sync controls.
    assert "librenms_server_info.server_key" in block
    assert "get('server_key') || ''" not in block


def _superuser_request(query=""):
    request = RequestFactory().get(f"/{query}")
    # get_or_create, not create: a test that captures twice (the per-capture salt one) reuses it.
    request.user, _ = get_user_model().objects.get_or_create(
        username="cap-admin", defaults={"is_superuser": True, "is_active": True}
    )
    return request


def _view_with_api(api):
    view = CaptureDataShapeView()
    view._librenms_api = api
    return view


def _run_capture(view, server, device, *, query="?server_key=test"):
    """Run capture through real API rebind, ID lookup, and device sync with only config patched."""
    servers_config = {
        "test": {"librenms_url": server.url, "api_token": "test-token", "cache_timeout": 0, "verify_ssl": False}
    }
    with patch("netbox_librenms_plugin.librenms_api.get_plugin_config") as mock_cfg:
        mock_cfg.side_effect = lambda _plugin, key: servers_config if key == "servers" else None
        return view.get(_superuser_request(query), device_id=device.pk)


@pytest.mark.django_db
def test_capture_view_renders_modal_with_anonymized_json_and_issue_link(recording_server):
    """The modal shows the novelty verdict, the anonymized (PII-scrubbed) JSON, and a prefilled issue link."""
    server, api = recording_server(load_recording("cisco-stackwise-3member"))
    device = make_device("cap-dev", librenms_cf={"test": {"id": 1000}})
    view = _view_with_api(api)

    response = _run_capture(view, server, device)

    html = response.content.decode()
    assert "Capture data shape: cap-dev" in html
    assert "Anonymized recording" in html
    assert "issues/new" in html and "data-shape" in html  # prefilled GitHub issue link
    # The JSON in the modal is anonymized — the original serial must be gone, a pseudonym present.
    assert "SN-a1b2c3" not in html
    assert "SN-" in html
    # Clean recording → the PII safety-net shows the all-clear, not a warning.
    assert "No residual IP / MAC / email" in html


@pytest.mark.django_db
def test_capture_view_reports_novelty_verdict(recording_server):
    """A shape already covered by the manifest is reported as likely-covered."""
    server, api = recording_server(load_recording("cisco-stackwise-3member"))
    device = make_device("cap-dev-2", librenms_cf={"test": {"id": 1000}})
    view = _view_with_api(api)

    response = _run_capture(view, server, device)

    assert "Likely already covered" in response.content.decode()


@pytest.mark.django_db
def test_capture_view_reports_similar_for_related_os_variant(recording_server):
    """A same-shape device on a related OS variant is flagged 'similar', not 'already covered'."""
    rec = load_recording("cisco-stackwise-3member")  # ios VC, covered by the manifest
    # Same VC shape, but a different cisco OS variant (iosxr) — behaves differently, so 'similar'.
    rec["responses"]["GET /api/v0/devices/1000"]["devices"][0]["os"] = "iosxr"
    rec["meta"] = {**rec.get("meta", {}), "os": "iosxr"}
    server, api = recording_server(rec)
    device = make_device("cap-dev-similar", librenms_cf={"test": {"id": 1000}})
    view = _view_with_api(api)

    response = _run_capture(view, server, device)

    html = response.content.decode()
    assert "Similar to an existing shape" in html
    assert "Likely already covered" not in html
    assert "cisco-stackwise-3member" in html  # the closest neighbour is surfaced


@pytest.mark.django_db
def test_capture_view_compresses_redundant_ports(recording_server):
    """The capture pipeline trims redundant high-cardinality ports before anonymizing/displaying."""
    ports = [
        {"port_id": 1, "ifName": "Bundle-Ether1", "ifType": "ieee8023adLag"},
        {"port_id": 2, "ifName": "GigabitEthernet0/0/0/1", "ifType": "ethernetCsmacd"},
    ]
    # 60 same-shape access ports (ethernetCsmacd + VLAN), no relationships — pure cardinality.
    for i in range(60):
        ports.append(
            {"port_id": 100 + i, "ifName": f"GigabitEthernet0/0/1/{i}", "ifType": "ethernetCsmacd", "ifVlan": 10}
        )
    rec = {
        "schema_version": 1,
        "name": "big",
        "device_id": 1000,
        "meta": {"os": "iosxr"},
        "responses": {
            "GET /api/v0/devices/1000": {"status": "ok", "devices": [{"device_id": 1000, "os": "iosxr"}]},
            "GET /api/v0/devices/1000/ports": {"status": "ok", "ports": ports},
            "GET /api/v0/devices/1000/port_stack": {
                "status": "ok",
                "mappings": [{"high_port_id": 2, "low_port_id": 1}],
            },
        },
    }
    server, api = recording_server(rec)
    device = make_device("cap-big", librenms_cf={"test": {"id": 1000}})
    view = _view_with_api(api)

    response = _run_capture(view, server, device)

    html = response.content.decode()
    # The displayed JSON is HTML-escaped (quotes → &quot;), so assert on quote-free substrings.
    # The meta records that compression ran (the key is only added when ports are dropped)...
    assert "compressed_ports" in html
    # ...and only 3 port records remain (down from 62): LAG aggregate + its member + one access rep.
    # ifName appears once per port and nowhere else in the recording.
    assert html.count("ifName") == 3


@pytest.mark.django_db
def test_capture_view_issue_url_uses_anonymized_name_not_device_name(recording_server):
    """The prefilled GitHub issue title must carry the anonymized recording name, not the real device name."""
    import re

    server, api = recording_server(load_recording("cisco-stackwise-3member"))
    device = make_device("supersecret-rtr01.dc1.internal", librenms_cf={"test": {"id": 1000}})
    view = _view_with_api(api)

    response = _run_capture(view, server, device)

    html = response.content.decode()
    issue_url = re.search(r"https://github.com/[^\"']*issues/new[^\"']*", html)
    assert issue_url, "issue link not found"
    # The real device name must NOT reach the public issue URL; the anonymized shape name is used.
    assert "supersecret-rtr01" not in issue_url.group(0)
    assert "shape" in issue_url.group(0)


@pytest.mark.django_db
def test_capture_view_download_filename_uses_anonymized_name_not_device_name(recording_server):
    """The downloaded file's name must carry the anonymized shape name, not the real device name."""
    import re

    server, api = recording_server(load_recording("cisco-stackwise-3member"))
    device = make_device("supersecret-rtr01.dc1.internal", librenms_cf={"test": {"id": 1000}})
    view = _view_with_api(api)

    response = _run_capture(view, server, device)

    html = response.content.decode()
    filename = re.search(r'data-filename="([^"]*)"', html)
    assert filename, "data-filename attribute not found"
    # The file attached to the public issue must not leak the hostname the JSON scrubbed.
    assert "supersecret-rtr01" not in filename.group(1)
    assert filename.group(1).endswith(".json") and "shape" in filename.group(1)


@pytest.mark.django_db
def test_capture_view_includes_oob_controller_ports(recording_server):
    """A device with a linked OOB controller has the controller's ports captured into the recording."""
    rec = load_recording("cisco-stackwise-3member")  # host device 1000
    # The OOB controller (LibreNMS id 2500) is a separate device with its own ports route.
    rec["responses"]["GET /api/v0/devices/2500/ports"] = {
        "status": "ok",
        "ports": [
            {"port_id": 7001, "ifName": "bmc", "ifType": "ethernetCsmacd"},
            {"port_id": 7002, "ifName": "eth0", "ifType": "ethernetCsmacd"},
        ],
    }
    server, api = recording_server(rec)
    # Seed the device's librenms_id CF with an OOB controller under the 'test' server key. The real
    # get_librenms_id (CF read), rebind_api_for_server and get_librenms_oob all run — no owned patches.
    device = make_device("cap-oob", librenms_cf={"test": {"id": 1000, "oob": {"id": 2500, "type": "drac"}}})
    view = _view_with_api(api)

    response = _run_capture(view, server, device)

    html = response.content.decode()
    # The recording records meta.oob_id and the controller's own /ports route (quotes are HTML-escaped).
    assert "oob_id" in html
    assert "2500/ports" in html


@pytest.mark.django_db
def test_capture_view_errors_when_device_not_linked(recording_server):
    """A device with no LibreNMS id shows an error panel instead of capturing."""
    server, api = recording_server(load_recording("cisco-stackwise-3member"))
    # No librenms_id CF and no primary IP → the real get_librenms_id finds no stored id and no
    # API-discovery identity, so it returns None and the view shows the not-linked error.
    device = make_device("cap-dev-3")
    view = _view_with_api(api)

    response = _run_capture(view, server, device)

    html = response.content.decode()
    assert "not linked to LibreNMS" in html
    assert "Anonymized recording" not in html


@pytest.mark.django_db
def test_capture_view_errors_on_stale_server_key(recording_server):
    """A stale/unconfigured server key shows an error panel."""
    server, api = recording_server(load_recording("cisco-stackwise-3member"))
    device = make_device("cap-dev-4")
    view = _view_with_api(api)

    # 'ghost' is not in the configured servers (only 'test' is), so the real rebind_api_for_server
    # resolves to None and the view shows the stale-server error — no owned method patched.
    response = _run_capture(view, server, device, query="?server_key=ghost")

    assert "no longer configured" in response.content.decode()


@pytest.mark.django_db
def test_capture_view_warns_on_residual_pii(recording_server):
    """When anonymization leaves residual PII (an IP in a preserved label), the modal warns."""
    rec = load_recording("cisco-stackwise-3member")
    rec["responses"]["GET /api/v0/devices/1000"]["devices"][0]["sysName"] = "core"
    # Plant an IP in a PRESERVED label so it survives anonymization and trips the safety-net.
    root_key = "GET /api/v0/inventory/1000?entPhysicalContainedIn=0"
    rec["responses"][root_key]["inventory"][0]["entPhysicalName"] = "stack mgmt 10.7.8.9"
    server, api = recording_server(rec)
    device = make_device("cap-dev-5", librenms_cf={"test": {"id": 1000}})
    view = _view_with_api(api)

    response = _run_capture(view, server, device)

    html = response.content.decode()
    assert "possible PII value" in html
    assert "10.7.8.9" in html


@pytest.mark.django_db
def test_capture_view_errors_on_mid_capture_transport_failure(recording_server):
    """A mid-capture failure (capture raises RuntimeError) renders the error panel — an unhandled raise would 500, which htmx silently drops, leaving the Capture button apparently dead."""
    server, api = recording_server(load_recording("cisco-stackwise-3member"))
    device = make_device("cap-dev-transport", librenms_cf={"test": {"id": 1000}})
    view = _view_with_api(api)

    with patch(
        "netbox_librenms_plugin.views.data_shapes.capture_device_recording",
        side_effect=RuntimeError("Capture failed for 'devices/1000/ports': no HTTP response (status 0)"),
    ):
        response = _run_capture(view, server, device)

    assert response.status_code == 200
    html = response.content.decode()
    assert "Capture failed" in html
    assert "no HTTP response" in html
    assert "Anonymized recording" not in html


@pytest.mark.django_db
def test_capture_view_denies_device_outside_users_object_scope(recording_server):
    """Verify a constrained model grant cannot bypass object scope through a raw device primary key."""
    from core.models import ObjectType
    from dcim.models import Device
    from django.http import Http404
    from users.models import ObjectPermission

    server, api = recording_server(load_recording("cisco-stackwise-3member"))
    target = make_device("secret-rtr", librenms_cf={"test": {"id": 1000}})

    user = get_user_model().objects.create_user(username="scoped-viewer", password="x")
    op = ObjectPermission.objects.create(
        name="view-only-other-devices", actions=["view"], constraints={"name": "not-the-target"}
    )
    op.object_types.set([ObjectType.objects.get_for_model(Device)])
    op.users.set([user])
    user = get_user_model().objects.get(pk=user.pk)  # clear the per-request perm cache

    # The model-level gate the view runs first PASSES for this constrained grant...
    assert user.has_perm("dcim.view_device")
    # ...but the target device is NOT within the user's object scope.
    assert not Device.objects.restrict(user, "view").filter(pk=target.pk).exists()

    request = RequestFactory().get("/?server_key=test")
    request.user = user
    view = _view_with_api(api)

    servers_config = {
        "test": {"librenms_url": server.url, "api_token": "test-token", "cache_timeout": 0, "verify_ssl": False}
    }
    with patch("netbox_librenms_plugin.librenms_api.get_plugin_config") as mock_cfg:
        mock_cfg.side_effect = lambda _plugin, key: servers_config if key == "servers" else None
        # Out-of-scope id must 404 (fail-closed), never render the device's captured shape.
        with pytest.raises(Http404):
            view.get(request, device_id=target.pk)


@pytest.mark.django_db
def test_capture_view_salts_every_capture_freshly(recording_server):
    """The contributor publishes this file, so the pseudonyms must not be a plain hash of low-entropy values: a fresh per-capture salt makes two captures of the SAME device produce different pseudonyms (dictionary-hashing a candidate hostname no longer confirms it)."""
    import re

    server, api = recording_server(load_recording("cisco-stackwise-3member"))
    device = make_device("cap-dev-salt", librenms_cf={"test": {"id": 1000}})

    first = _run_capture(_view_with_api(api), server, device).content.decode()
    second = _run_capture(_view_with_api(api), server, device).content.decode()

    first_pseudonyms = set(re.findall(r"SN-[0-9a-f]{6}", first))
    second_pseudonyms = set(re.findall(r"SN-[0-9a-f]{6}", second))
    assert first_pseudonyms and second_pseudonyms
    assert first_pseudonyms.isdisjoint(second_pseudonyms)
