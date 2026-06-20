"""Captured LibreNMS data-shape recordings used as outcome tests.

Each ``*.json`` file in this directory is one device scenario: the LibreNMS API
responses captured verbatim (so the mock server can replay the exact shapes) plus
an ``expected`` block describing the outcome the sync logic should produce. A new
community-contributed JSON with an ``expected`` block becomes a passing test with
no new code -- see ``tests/test_recordings.py``.
"""

import json
from pathlib import Path

_DIR = Path(__file__).parent


def iter_recording_paths():
    """Return the sorted list of recording JSON file paths (excluding the novelty manifest)."""
    return sorted(p for p in _DIR.glob("*.json") if p.name != "manifest.json")


def load_recording(name: str) -> dict:
    """Load a single recording by name, with or without the ``.json`` suffix."""
    filename = name if name.endswith(".json") else f"{name}.json"
    return json.loads((_DIR / filename).read_text())


def iter_recordings() -> list[dict]:
    """Load and return every recording in this directory."""
    return [json.loads(p.read_text()) for p in iter_recording_paths()]
