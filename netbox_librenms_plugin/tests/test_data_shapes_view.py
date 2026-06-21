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


def _superuser_request(query=""):
    request = RequestFactory().get(f"/{query}")
    request.user = get_user_model().objects.create(username="cap-admin", is_superuser=True, is_active=True)
    return request


def _view_with_api(api):
    view = CaptureDataShapeView()
    view._librenms_api = api
    return view


@pytest.mark.django_db
def test_capture_view_renders_modal_with_anonymized_json_and_issue_link(recording_server):
    """The modal shows the novelty verdict, the anonymized (PII-scrubbed) JSON, and a prefilled issue link."""
    _server, api = recording_server(load_recording("cisco-stackwise-3member"))
    device = make_device("cap-dev")
    view = _view_with_api(api)

    with (
        patch.object(view, "rebind_api_for_server", return_value="test"),
        patch.object(api, "get_librenms_id", return_value=1000),
        patch("netbox_librenms_plugin.views.data_shapes.get_librenms_sync_device", return_value=None),
    ):
        response = view.get(_superuser_request("?server_key=test"), device_id=device.pk)

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
    _server, api = recording_server(load_recording("cisco-stackwise-3member"))
    device = make_device("cap-dev-2")
    view = _view_with_api(api)

    with (
        patch.object(view, "rebind_api_for_server", return_value="test"),
        patch.object(api, "get_librenms_id", return_value=1000),
        patch("netbox_librenms_plugin.views.data_shapes.get_librenms_sync_device", return_value=None),
    ):
        response = view.get(_superuser_request("?server_key=test"), device_id=device.pk)

    assert "Likely already covered" in response.content.decode()


@pytest.mark.django_db
def test_capture_view_reports_similar_for_related_os_variant(recording_server):
    """A same-shape device on a related OS variant is flagged 'similar', not 'already covered'."""
    rec = load_recording("cisco-stackwise-3member")  # ios VC, covered by the manifest
    # Same VC shape, but a different cisco OS variant (iosxr) — behaves differently, so 'similar'.
    rec["responses"]["GET /api/v0/devices/1000"]["devices"][0]["os"] = "iosxr"
    rec["meta"] = {**rec.get("meta", {}), "os": "iosxr"}
    _server, api = recording_server(rec)
    device = make_device("cap-dev-similar")
    view = _view_with_api(api)

    with (
        patch.object(view, "rebind_api_for_server", return_value="test"),
        patch.object(api, "get_librenms_id", return_value=1000),
        patch("netbox_librenms_plugin.views.data_shapes.get_librenms_sync_device", return_value=None),
    ):
        response = view.get(_superuser_request("?server_key=test"), device_id=device.pk)

    html = response.content.decode()
    assert "Similar to an existing shape" in html
    assert "Likely already covered" not in html
    assert "cisco-stackwise-3member" in html  # the closest neighbour is surfaced


@pytest.mark.django_db
def test_capture_view_errors_when_device_not_linked(recording_server):
    """A device with no LibreNMS id shows an error panel instead of capturing."""
    _server, api = recording_server(load_recording("cisco-stackwise-3member"))
    device = make_device("cap-dev-3")
    view = _view_with_api(api)

    with (
        patch.object(view, "rebind_api_for_server", return_value="test"),
        patch.object(api, "get_librenms_id", return_value=None),
        patch("netbox_librenms_plugin.views.data_shapes.get_librenms_sync_device", return_value=None),
    ):
        response = view.get(_superuser_request("?server_key=test"), device_id=device.pk)

    html = response.content.decode()
    assert "not linked to LibreNMS" in html
    assert "Anonymized recording" not in html


@pytest.mark.django_db
def test_capture_view_errors_on_stale_server_key(recording_server):
    """A stale/unconfigured server key shows an error panel."""
    _server, api = recording_server(load_recording("cisco-stackwise-3member"))
    device = make_device("cap-dev-4")
    view = _view_with_api(api)

    with patch.object(view, "rebind_api_for_server", return_value=None):
        response = view.get(_superuser_request("?server_key=ghost"), device_id=device.pk)

    assert "no longer configured" in response.content.decode()


@pytest.mark.django_db
def test_capture_view_warns_on_residual_pii(recording_server):
    """When anonymization leaves residual PII (an IP in a preserved label), the modal warns."""
    rec = load_recording("cisco-stackwise-3member")
    rec["responses"]["GET /api/v0/devices/1000"]["devices"][0]["sysName"] = "core"
    # Plant an IP in a PRESERVED label so it survives anonymization and trips the safety-net.
    root_key = "GET /api/v0/inventory/1000?entPhysicalContainedIn=0"
    rec["responses"][root_key]["inventory"][0]["entPhysicalName"] = "stack mgmt 10.7.8.9"
    _server, api = recording_server(rec)
    device = make_device("cap-dev-5")
    view = _view_with_api(api)

    with (
        patch.object(view, "rebind_api_for_server", return_value="test"),
        patch.object(api, "get_librenms_id", return_value=1000),
        patch("netbox_librenms_plugin.views.data_shapes.get_librenms_sync_device", return_value=None),
    ):
        response = view.get(_superuser_request("?server_key=test"), device_id=device.pk)

    html = response.content.decode()
    assert "possible PII value" in html
    assert "10.7.8.9" in html
