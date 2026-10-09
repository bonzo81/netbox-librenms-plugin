"""Harness checks for the ``tests/e2e`` suite, which sits outside ``testpaths``."""

import re
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parents[2] / "tests" / "e2e"
SOURCE_SUFFIXES = {".py", ".sh", ".yml", ".ini", ".txt"}


def test_e2e_run_instructions_carry_no_developer_home_path():
    """Every contributor must be able to run the documented command."""
    sources = sorted(
        path
        for path in E2E_DIR.rglob("*")
        if path.is_file() and (path.suffix in SOURCE_SUFFIXES or path.name == "Dockerfile")
    )
    assert sources, "the scan found no e2e source, so the check below would pass vacuously"

    leaks = [
        f"{path.relative_to(E2E_DIR)}:{number}"
        for path in sources
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if re.search(r"/(?:home|Users)/[A-Za-z0-9._-]+/", line)
    ]

    assert not leaks, f"absolute developer path in the e2e sources: {leaks}"
