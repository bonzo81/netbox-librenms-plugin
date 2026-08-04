"""
In-plugin "Capture data shape" view (issue #95).

Captures a device's LibreNMS responses, anonymizes and fingerprints them, classifies novelty
against the bundled shapes, and renders a modal offering the anonymized JSON (to copy/download)
plus a prefilled GitHub issue link — lowering the bar for community-contributed data shapes to
near zero. Read-only: it hits LibreNMS but mutates nothing in NetBox.
"""

import json
import secrets
from urllib.parse import urlencode

from dcim.models import Device
from django.shortcuts import render
from django.views import View

from netbox_librenms_plugin.data_shapes import recordings_store
from netbox_librenms_plugin.data_shapes.anonymize import anonymize_recording, find_pii
from netbox_librenms_plugin.data_shapes.capture import capture_device_recording
from netbox_librenms_plugin.data_shapes.compress import compress_recording
from netbox_librenms_plugin.data_shapes.signature import classify_novelty, compute_shape_signature
from netbox_librenms_plugin.utils import get_librenms_oob, get_librenms_sync_device
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

        # Resolve through the RESTRICTED queryset, not the plain manager: require_object_permissions
        # only checks model-level has_perm (no instance), which a site-constrained view_device grant
        # passes — so a plain get_object_or_404 would let such a user capture (and see the modal's
        # find_pii values for) any device by raw pk. An out-of-scope id 404s like a nonexistent one.
        device = self.restrict_object_or_404(Device, pk=device_id)

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

        # Include a linked OOB controller's ports (a separate LibreNMS device the interfaces view
        # merges into the host) so the submitted shape is complete for OOB hosts.
        oob = get_librenms_oob(sync_device, server_key=server_key)
        try:
            recording = capture_device_recording(
                self.librenms_api,
                librenms_id,
                name=f"{device.name}-shape",
                description=f"Captured from {device.name}.",
                oob_id=oob["id"] if oob and oob.get("id") else None,
            )
        except RuntimeError as exc:
            # capture deliberately raises on an incomplete capture (transport failure or an
            # error status on a required structural route, e.g. a stale librenms_id). An
            # unhandled raise would 500 — and htmx doesn't swap non-2xx responses, so the
            # Capture button would just appear dead. Render the error panel built for this.
            return self._error(request, device, f"Capture failed: {exc}")
        # Trim redundant high-cardinality ports BEFORE anonymizing — on the raw port names, which is
        # exactly what relationship resolution reads — so the shape stays intact while the recording
        # the contributor submits is small and reviewable.
        recording = compress_recording(recording)
        # A fresh high-entropy salt per capture. The pseudonyms only need to be consistent WITHIN
        # one recording (that is what keeps cross-references matching), and the contributor
        # publishes this file: with the empty default salt, a sha256 of a low-entropy value like a
        # hostname is recoverable by hashing candidates from a dictionary.
        anonymized = anonymize_recording(recording, salt=secrets.token_hex(16))
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
                # The download filename must use the ANONYMIZED recording name (os-<hash>-shape-<hash>),
                # never the real device name — the contributor attaches this file to the public issue,
                # so its name must not leak the hostname the JSON scrubbed (mirrors the issue title).
                "anonymized_name": anonymized.get("name", ""),
                "issue_url": self._issue_url(anonymized.get("name", "")),
            },
        )

    def _error(self, request, device, message):
        return render(request, self.template_name, {"object": device, "error": message})

    def _issue_url(self, anonymized_name):
        """
        Build a prefilled GitHub issue URL targeting the data-shape issue form.

        Only ``template``/``title``/``labels`` are prefilled — the data-shape.yml issue *form*
        ignores a ``body`` query param, and the anonymized recording is intentionally kept out of
        the URL (a full recording would blow past URL length limits). The user pastes the JSON
        (copy/download from the modal) into the form's "Anonymized recording" field. The title uses
        the *anonymized* recording name (``os-<hash>-shape-<hash>``), never the real device name —
        the title goes into the public issue URL, so it must not leak infra the JSON scrubbed.
        """
        params = {
            "template": "data-shape.yml",
            "title": f"Data shape: {anonymized_name}" if anonymized_name else "Data shape submission",
            "labels": "data-shape",
        }
        return f"{ISSUE_BASE_URL}?{urlencode(params)}"
