"""
Locate and load the bundled data-shape recordings and the novelty manifest.

Single source of truth for where recordings live and how they're loaded, shared by the test
suite, the management command (which writes the manifest), and the in-plugin capture view (which
reads it for the novelty verdict). The recordings live INSIDE the package
(``data_shapes/recordings/``) — not under ``tests/`` — so they ship in the wheel (the tests
package is excluded from the build). Reading the manifest is best-effort: a missing manifest
yields an empty list so the novelty check degrades to "new" rather than erroring.
"""

import json
from pathlib import Path

# data_shapes/recordings/ (a data directory inside the data_shapes package, so it ships).
RECORDINGS_DIR = Path(__file__).resolve().parent / "recordings"
MANIFEST_PATH = RECORDINGS_DIR / "manifest.json"
MANIFEST_NAME = MANIFEST_PATH.name


def iter_recording_paths():
    """Return the sorted list of recording JSON file paths (excluding the novelty manifest)."""
    return sorted(p for p in RECORDINGS_DIR.glob("*.json") if p.name != MANIFEST_NAME)


def load_recording(name: str) -> dict:
    """Load a single recording by name, with or without the ``.json`` suffix."""
    filename = name if name.endswith(".json") else f"{name}.json"
    # Constrain the resolved path to RECORDINGS_DIR so a name like "../../secrets" can't escape
    # the recordings directory (defensive: callers pass fixed fixture names today, but keep the
    # loader safe if a name ever becomes caller-derived).
    base_dir = RECORDINGS_DIR.resolve()
    candidate = (base_dir / filename).resolve()
    try:
        candidate.relative_to(base_dir)
    except ValueError as exc:
        raise ValueError(f"Invalid recording name: {name!r}") from exc
    return json.loads(candidate.read_text())


def iter_recordings() -> list[dict]:
    """Load and return every bundled recording (excluding the manifest), sorted by path."""
    return [json.loads(p.read_text()) for p in iter_recording_paths()]


# Back-compat alias used by the management command / novelty manifest builder.
load_bundled_recordings = iter_recordings


def load_manifest():
    """Load the novelty manifest, or [] when it has not been generated/shipped."""
    if not MANIFEST_PATH.exists():
        return []
    try:
        manifest = json.loads(MANIFEST_PATH.read_text())
    except (OSError, ValueError):
        # read_text() can raise OSError (permission/IO), not just a JSON ValueError. The
        # contract here is best-effort: return [] rather than break every caller.
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
