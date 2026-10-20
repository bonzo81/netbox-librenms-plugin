"""Harness checks for the ``tests/e2e`` suite, which sits outside ``testpaths``."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_DIR = REPO_ROOT / "tests" / "e2e"
SOURCE_SUFFIXES = {".py", ".sh", ".yml", ".ini", ".txt"}


def _tracked_e2e_files(repo_root: Path = REPO_ROOT) -> list[Path]:
    """Return the git-tracked files under ``tests/e2e``; local output such as Playwright traces is not a source."""
    listing = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z", "--", "tests/e2e"], capture_output=True, check=True
    ).stdout.decode()
    paths = (repo_root / name for name in listing.split("\0") if name)
    # The index still lists a file whose deletion is not staged.
    return [path for path in paths if path.is_file()]


# A container can mount a worktree whose .git file points at a host path that git cannot read.
GIT_READS_CHECKOUT = (
    shutil.which("git") is not None
    and subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "--git-dir"], capture_output=True).returncode == 0
)


@pytest.mark.skipif(
    not GIT_READS_CHECKOUT,
    reason="git is not installed or cannot read this checkout, so the tracked e2e sources are unknown",
)
def test_e2e_run_instructions_carry_no_developer_home_path():
    """Every contributor must be able to run the documented command."""
    sources = sorted(
        path for path in _tracked_e2e_files() if path.suffix in SOURCE_SUFFIXES or path.name == "Dockerfile"
    )
    assert sources, "the scan found no e2e source, so the check below would pass vacuously"

    leaks = [
        f"{path.relative_to(E2E_DIR)}:{number}"
        for path in sources
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if re.search(r"/(?:home|Users)/[A-Za-z0-9._-]+/", line)
    ]

    assert not leaks, f"absolute developer path in the e2e sources: {leaks}"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_tracked_e2e_files_skip_a_deletion_that_is_not_staged(tmp_path):
    """The index still lists a file that was deleted on disk; the scan must not try to read it."""

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], capture_output=True, check=True)

    e2e = tmp_path / "tests" / "e2e"
    e2e.mkdir(parents=True)
    (e2e / "kept.py").write_text("")
    (e2e / "deleted.py").write_text("")
    git("init", "-q")
    git("add", "tests/e2e")
    (e2e / "deleted.py").unlink()

    assert _tracked_e2e_files(tmp_path) == [e2e / "kept.py"]
