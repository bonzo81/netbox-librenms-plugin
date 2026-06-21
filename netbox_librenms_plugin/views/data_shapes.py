"""
In-plugin "Capture data shape" view (issue #95).

Captures a device's LibreNMS responses, anonymizes and fingerprints them, classifies novelty
against the bundled shapes, and renders a modal offering the anonymized JSON (to copy/download)
plus a prefilled GitHub issue link — lowering the bar for community-contributed data shapes to
near zero. Read-only: it hits LibreNMS but mutates nothing in NetBox.
"""

import json
from urllib.parse import urlencode

from dcim.models import Device
from django.shortcuts import get_object_or_404, render
from django.views import View

from netbox_librenms_plugin.data_shapes import recordings_store
from netbox_librenms_plugin.data_shapes.anonymize import anonymize_recording, find_pii
from netbox_librenms_plugin.data_shapes.capture import capture_device_recording
from netbox_librenms_plugin.data_shapes.signature import classify_novelty, compute_shape_signature
from netbox_librenms_plugin.utils import get_librenms_sync_device
from netbox_librenms_plugin.views.mixins import (
    LibreNMSAPIMixin,
    LibreNMSPermissionMixin,
    NetBoxObjectPermissionMixin,
)

# The upstream project where data-shape submissions are collected (see pyproject [project.urls]).
ISSUE_BASE_URL = "https://github.com/bonzo81/netbox-librenms-plugin/issues/new"


class CaptureDataShapeView(LibreNMSPermissionMixin, NetBoxObjectPermissionMixin, LibreNMSAPIMixin, View):
    """Capture, anonymize, and fingerprint a device's LibreNMS data shape for community submission."""

    required_object_permissions = {"GET": [("view", Device)]}
    template_name = "netbox_librenms_plugin/htmx/capture_data_shape.html"

    def get(self, request, device_id):
        """Render the capture modal for the device, or an error panel when capture isn't possible."""
        self.request = request
        if error := self.require_object_permissions("GET"):
            return error

        device = get_object_or_404(Device, pk=device_id)

        # Scope the LibreNMS client + id lookup to the server the user is viewing (multi-server).
        server_key = self.rebind_api_for_server(request.GET.get("server_key"))
        if server_key is None:
            return self._error(request, device, "Selected LibreNMS server is no longer configured.")

        sync_device = get_librenms_sync_device(device, server_key=server_key) or device
        librenms_id = self.librenms_api.get_librenms_id(sync_device)
        if not librenms_id:
            return self._error(
                request, device, "This device is not linked to LibreNMS, so there is no data shape to capture."
            )

        recording = capture_device_recording(
            self.librenms_api,
            librenms_id,
            name=f"{device.name}-shape",
            description=f"Captured from {device.name}.",
        )
        anonymized = anonymize_recording(recording)
        signature = compute_shape_signature(anonymized)
        novelty = classify_novelty(signature, recordings_store.load_manifest())
        residual_pii = find_pii(anonymized)
        recording_json = json.dumps(anonymized, indent=2)

        return render(
            request,
            self.template_name,
            {
                "object": device,
                "server_key": server_key,
                "novelty": novelty,
                "signature": signature,
                "recording_json": recording_json,
                "residual_pii": residual_pii,
                "issue_url": self._issue_url(device),
            },
        )

    def _error(self, request, device, message):
        return render(request, self.template_name, {"object": device, "error": message})

    def _issue_url(self, device):
        """
        Build a prefilled GitHub issue URL targeting the data-shape issue form.

        Only ``template``/``title``/``labels`` are prefilled — the data-shape.yml issue *form*
        ignores a ``body`` query param, and the anonymized recording is intentionally kept out of
        the URL (a full recording would blow past URL length limits). The user pastes the JSON
        (copy/download from the modal) into the form's "Anonymized recording" field.
        """
        params = {
            "template": "data-shape.yml",
            "title": f"Data shape: {device.name}",
            "labels": "data-shape",
        }
        return f"{ISSUE_BASE_URL}?{urlencode(params)}"
