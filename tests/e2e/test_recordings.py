"""The harness reads a recording the way the stub serves it."""

import json

import pytest

from . import conftest
from .conftest import recorded

ROUTE = "/api/v0/devices/7"


@pytest.mark.parametrize(
    ("stored", "body"),
    [
        ([200, {"status": "ok"}], [200, {"status": "ok"}]),
        ({"__http_status__": 404, "body": {"status": "error"}}, {"status": "error"}),
        ({"status": "ok", "devices": []}, {"status": "ok", "devices": []}),
    ],
    ids=["plain-list-body", "tagged-error", "plain-dict-body"],
)
def test_recorded_returns_the_body_the_stub_serves(tmp_path, monkeypatch, stored, body):
    (tmp_path / "shape.json").write_text(json.dumps({"responses": {f"GET {ROUTE}": stored}}))
    monkeypatch.setattr(conftest, "RECORDINGS_DIR", tmp_path)

    assert recorded("shape", ROUTE) == body
