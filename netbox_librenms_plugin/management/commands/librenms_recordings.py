"""
Manage LibreNMS data-shape recordings: validate a submission, rebuild the manifest, or list them.

This is the maintainer/CI entry point for the capture→anonymize→replay pipeline (issue #95):

* ``--validate PATH`` — schema-check a (community-submitted) recording, re-run the PII safety-net
  on it, and report its novelty vs the bundled shapes. Exits non-zero on a schema or PII failure
  so CI can gate on it.
* ``--rebuild-manifest`` — regenerate ``data_shapes/recordings/manifest.json`` from the bundled
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
        """Register the mutually-exclusive --validate / --rebuild-manifest / --list actions."""
        # The three actions are alternatives, not combinable: handle() runs only the first truthy
        # one, so allowing them together is silently ambiguous in CI. Enforce exactly-one-or-none
        # at parse time (not required, so a bare invocation still falls through to print_help).
        action_group = parser.add_mutually_exclusive_group()
        action_group.add_argument(
            "--validate", metavar="PATH", help="Validate a recording JSON: schema + PII sweep + novelty."
        )
        action_group.add_argument(
            "--rebuild-manifest",
            action="store_true",
            help="Rebuild data_shapes/recordings/manifest.json from the bundled recordings.",
        )
        action_group.add_argument("--list", action="store_true", help="List bundled recordings with their signatures.")

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
            # Report only the kind + JSON path, never the raw value — this command runs in CI, and
            # echoing p['value'] would leak the exact sensitive material the scan exists to catch.
            lines = "\n  - ".join(f"{p['kind']} at {p['path']}" for p in pii)
            raise CommandError(f"Recording still contains PII (anonymize before submitting):\n  - {lines}")

        signature = compute_shape_signature(recording)
        # Use the shipped manifest.json (recordings_store.load_manifest), not a manifest rebuilt from
        # load_bundled_recordings(): the recordings are dev/test fixtures NOT packaged in the wheel,
        # so in a wheel install build_manifest(load_bundled_recordings()) would be empty and classify
        # every submission as novel. This mirrors the runtime capture view's novelty contract.
        try:
            manifest = recordings_store.load_manifest()
        except RuntimeError as exc:
            raise CommandError(str(exc)) from exc
        verdict = classify_novelty(signature, manifest)
        self.stdout.write(self.style.SUCCESS(f"OK: '{recording['name']}' is schema-valid and PII-clean."))
        self.stdout.write(f"Novelty: {verdict['verdict']} ({verdict['why']}).")

    def _rebuild_manifest(self):
        recordings = recordings_store.load_bundled_recordings()
        if not recordings:
            # Refuse to overwrite the shipped manifest with an empty one. The recordings are dev/test
            # fixtures that are NOT packaged in the wheel (only manifest.json ships), so a wheel
            # install has no recordings to rebuild from — writing "[]" here would wipe the manifest
            # the in-plugin novelty view reads, making every future capture report "new". Rebuild
            # only from a source checkout where the recordings are present.
            raise CommandError(
                f"No recordings found under {recordings_store.RECORDINGS_DIR}; refusing to overwrite "
                f"{recordings_store.MANIFEST_PATH.name} with an empty manifest. Run --rebuild-manifest "
                f"from a source checkout (the recordings are not packaged in the wheel)."
            )
        manifest = build_manifest(recordings)
        # Write atomically (tmp + replace): a direct write_text truncates the shipped manifest
        # first, so a crash mid-write would make novelty classification unavailable until a rebuild.
        tmp_path = recordings_store.MANIFEST_PATH.with_name(recordings_store.MANIFEST_PATH.name + ".tmp")
        tmp_path.write_text(json.dumps(manifest, indent=2) + "\n")
        tmp_path.replace(recordings_store.MANIFEST_PATH)
        self.stdout.write(
            self.style.SUCCESS(f"Wrote {len(manifest)} signature(s) to {recordings_store.MANIFEST_PATH}.")
        )

    def _list(self):
        for recording in recordings_store.load_bundled_recordings():
            signature = compute_shape_signature(recording)
            self.stdout.write(f"{recording.get('name')}: {json.dumps(signature)}")
