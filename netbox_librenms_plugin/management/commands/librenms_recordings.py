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

from netbox_librenms_plugin.data_shapes.anonymize import find_pii
from netbox_librenms_plugin.data_shapes.signature import build_manifest, classify_novelty, compute_shape_signature

# tests/recordings/ relative to this file (management/commands/ -> plugin root -> tests/recordings).
RECORDINGS_DIR = Path(__file__).resolve().parents[2] / "tests" / "recordings"
MANIFEST_PATH = RECORDINGS_DIR / "manifest.json"


def recording_schema_errors(recording):
    """
    Return a list of human-readable schema problems with a recording (empty when valid).

    Args:
        recording: The parsed recording object.

    Returns:
        list[str]: One message per violated schema expectation.
    """
    errors = []
    if not isinstance(recording, dict):
        return ["recording must be a JSON object"]
    if recording.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if not isinstance(recording.get("name"), str) or not recording.get("name"):
        errors.append("name must be a non-empty string")
    if not isinstance(recording.get("device_id"), int):
        errors.append("device_id must be an integer")
    responses = recording.get("responses")
    if not isinstance(responses, dict) or not responses:
        errors.append("responses must be a non-empty object")
    return errors


def load_bundled_recordings():
    """Load every bundled recording JSON (excluding the manifest), sorted by path."""
    recordings = []
    for path in sorted(RECORDINGS_DIR.glob("*.json")):
        if path.name == MANIFEST_PATH.name:
            continue
        recordings.append(json.loads(path.read_text()))
    return recordings


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

        errors = recording_schema_errors(recording)
        if errors:
            raise CommandError("Invalid recording schema:\n  - " + "\n  - ".join(errors))

        pii = find_pii(recording)
        if pii:
            lines = "\n  - ".join(f"{p['kind']} {p['value']} at {p['path']}" for p in pii)
            raise CommandError(f"Recording still contains PII (anonymize before submitting):\n  - {lines}")

        signature = compute_shape_signature(recording)
        verdict = classify_novelty(signature, build_manifest(load_bundled_recordings()))
        self.stdout.write(self.style.SUCCESS(f"OK: '{recording['name']}' is schema-valid and PII-clean."))
        self.stdout.write(f"Novelty: {verdict['verdict']} ({verdict['why']}).")

    def _rebuild_manifest(self):
        manifest = build_manifest(load_bundled_recordings())
        MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")
        self.stdout.write(self.style.SUCCESS(f"Wrote {len(manifest)} signature(s) to {MANIFEST_PATH}."))

    def _list(self):
        for recording in load_bundled_recordings():
            signature = compute_shape_signature(recording)
            self.stdout.write(f"{recording.get('name')}: {json.dumps(signature)}")
