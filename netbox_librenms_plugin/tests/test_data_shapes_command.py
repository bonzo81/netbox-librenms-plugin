"""Tests for the librenms_recordings management command (validate / rebuild-manifest)."""

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from netbox_librenms_plugin.data_shapes.anonymize import anonymize_recording
from netbox_librenms_plugin.management.commands import librenms_recordings as cmd
from netbox_librenms_plugin.tests.recordings import load_recording


def _run(**kwargs):
    out = StringIO()
    call_command("librenms_recordings", stdout=out, **kwargs)
    return out.getvalue()


def test_validate_clean_recording_reports_novelty(tmp_path):
    """A schema-valid, PII-clean recording validates and reports a novelty verdict."""
    rec = anonymize_recording(load_recording("cisco-stackwise-3member"))
    path = tmp_path / "rec.json"
    path.write_text(json.dumps(rec))

    output = _run(validate=str(path))

    assert "schema-valid and PII-clean" in output
    assert "Novelty: likely-covered" in output


def test_validate_rejects_residual_pii(tmp_path):
    """A recording with residual PII (an IP in a preserved label) is rejected."""
    rec = load_recording("cisco-stackwise-3member")
    rec["responses"]["GET /api/v0/devices/1000"]["devices"][0]["entPhysicalName"] = "core 10.7.8.9"
    rec = anonymize_recording(rec)  # entPhysicalName is preserved, so the IP survives
    path = tmp_path / "rec.json"
    path.write_text(json.dumps(rec))

    with pytest.raises(CommandError, match="contains PII"):
        _run(validate=str(path))


def test_validate_rejects_bad_schema(tmp_path):
    """A recording missing required schema fields is rejected before any PII/novelty work."""
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"name": "x", "responses": {}}))  # no schema_version/device_id, empty responses

    with pytest.raises(CommandError, match="Invalid recording schema"):
        _run(validate=str(path))


def test_rebuild_manifest_writes_signatures(tmp_path, monkeypatch):
    """--rebuild-manifest writes one {name, signature} entry per bundled recording."""
    # Point the command at an isolated recordings dir so the real repo file isn't clobbered.
    rec_dir = tmp_path / "recordings"
    rec_dir.mkdir()
    for name in ("cisco-stackwise-3member", "juniper-vc-2member", "cisco-lag-and-subinterface"):
        (rec_dir / f"{name}.json").write_text(json.dumps(load_recording(name)))
    monkeypatch.setattr(cmd, "RECORDINGS_DIR", rec_dir)
    monkeypatch.setattr(cmd, "MANIFEST_PATH", rec_dir / "manifest.json")

    output = _run(**{"rebuild_manifest": True})

    manifest = json.loads((rec_dir / "manifest.json").read_text())
    assert {e["name"] for e in manifest} == {
        "cisco-stackwise-3member",
        "juniper-vc-2member",
        "cisco-lag-and-subinterface",
    }
    assert all("signature" in e for e in manifest)
    assert "Wrote 3 signature(s)" in output


def test_rebuild_manifest_excludes_manifest_itself(tmp_path, monkeypatch):
    """A pre-existing manifest.json is not loaded back in as a recording on rebuild."""
    rec_dir = tmp_path / "recordings"
    rec_dir.mkdir()
    (rec_dir / "cisco-stackwise-3member.json").write_text(json.dumps(load_recording("cisco-stackwise-3member")))
    (rec_dir / "manifest.json").write_text(json.dumps([{"name": "stale", "signature": {}}]))
    monkeypatch.setattr(cmd, "RECORDINGS_DIR", rec_dir)
    monkeypatch.setattr(cmd, "MANIFEST_PATH", rec_dir / "manifest.json")

    _run(**{"rebuild_manifest": True})

    manifest = json.loads((rec_dir / "manifest.json").read_text())
    assert [e["name"] for e in manifest] == ["cisco-stackwise-3member"]
