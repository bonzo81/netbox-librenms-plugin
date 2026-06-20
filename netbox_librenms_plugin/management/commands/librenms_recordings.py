"""
Manage LibreNMS data-shape recordings: validate a submission, rebuild the manifest, or list them.

This is the maintainer/CI entry point for the capture→anonymize→replay pipeline (issue #95):

* ``--validate PATH`` — schema-check a (community-submitted) recording, re-run the PII safety-net
  on it, and report its novelty vs the bundled shapes. Exits non-zero on a schema or PII failure
  so CI can gate on it.
* ``--rebuild-manifest`` — regenerate ``tests/recordings/manifest.json`` from the bundled
  recordings (the novelty reference set).
* ``--list`` — print each bundled recording with its computed signature.
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from netbox_librenms_plugin.data_shapes import recordings_store
from netbox_librenms_plugin.data_shapes.anonymize import find_pii
from netbox_librenms_plugin.data_shapes.signature import build_manifest, classify_novelty, compute_shape_signature


class Command(BaseCommand):
    """Validate / manage LibreNMS data-shape recordings."""

    help = "Validate, manifest, and list LibreNMS data-shape recordings (issue #95)."

    def add_arguments(self, parser):
        """Register the mutually-useful --validate / --rebuild-manifest / --list options."""
        parser.add_argument(
            "--validate", metavar="PATH", help="Validate a recording JSON: schema + PII sweep + novelty."
        )
        parser.add_argument(
            "--rebuild-manifest",
            action="store_true",
            help="Rebuild tests/recordings/manifest.json from the bundled recordings.",
        )
        parser.add_argument("--list", action="store_true", help="List bundled recordings with their signatures.")

    def handle(self, *args, **options):
        """Dispatch to the requested action (validate / rebuild-manifest / list)."""
        if options["validate"]:
            self._validate(options["validate"])
        elif options["rebuild_manifest"]:
            self._rebuild_manifest()
        elif options["list"]:
            self._list()
        else:
            self.print_help("manage.py", "librenms_recordings")

    def _validate(self, path):
        try:
            recording = json.loads(Path(path).read_text())
        except (OSError, ValueError) as exc:
            raise CommandError(f"Could not read recording {path}: {exc}") from exc

        errors = recordings_store.recording_schema_errors(recording)
        if errors:
            raise CommandError("Invalid recording schema:\n  - " + "\n  - ".join(errors))

        pii = find_pii(recording)
        if pii:
            lines = "\n  - ".join(f"{p['kind']} {p['value']} at {p['path']}" for p in pii)
            raise CommandError(f"Recording still contains PII (anonymize before submitting):\n  - {lines}")

        signature = compute_shape_signature(recording)
        verdict = classify_novelty(signature, build_manifest(recordings_store.load_bundled_recordings()))
        self.stdout.write(self.style.SUCCESS(f"OK: '{recording['name']}' is schema-valid and PII-clean."))
        self.stdout.write(f"Novelty: {verdict['verdict']} ({verdict['why']}).")

    def _rebuild_manifest(self):
        manifest = build_manifest(recordings_store.load_bundled_recordings())
        recordings_store.MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")
        self.stdout.write(
            self.style.SUCCESS(f"Wrote {len(manifest)} signature(s) to {recordings_store.MANIFEST_PATH}.")
        )

    def _list(self):
        for recording in recordings_store.load_bundled_recordings():
            signature = compute_shape_signature(recording)
            self.stdout.write(f"{recording.get('name')}: {json.dumps(signature)}")
