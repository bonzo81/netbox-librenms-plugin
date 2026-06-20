"""
Locate and load the bundled data-shape recordings and the novelty manifest.

Single source of truth for where recordings live (``tests/recordings/``) so the management
command (which writes the manifest) and the in-plugin capture view (which reads it for the
novelty verdict) agree. Reading is best-effort: a missing manifest yields an empty list so the
novelty check degrades to "new" rather than erroring.
"""

import json
from pathlib import Path

# data_shapes/ -> netbox_librenms_plugin/ -> tests/recordings/
RECORDINGS_DIR = Path(__file__).resolve().parents[1] / "tests" / "recordings"
MANIFEST_PATH = RECORDINGS_DIR / "manifest.json"
MANIFEST_NAME = MANIFEST_PATH.name


def load_bundled_recordings():
    """Load every bundled recording JSON (excluding the manifest), sorted by path."""
    recordings = []
    for path in sorted(RECORDINGS_DIR.glob("*.json")):
        if path.name == MANIFEST_NAME:
            continue
        recordings.append(json.loads(path.read_text()))
    return recordings


def load_manifest():
    """Load the novelty manifest, or [] when it has not been generated/shipped."""
    if not MANIFEST_PATH.exists():
        return []
    try:
        manifest = json.loads(MANIFEST_PATH.read_text())
    except ValueError:
        return []
    return manifest if isinstance(manifest, list) else []


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
