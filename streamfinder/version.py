"""The running version, numbered automatically from the commit messages.

Counted over the whole history (Conventional Commits): `feat:` raises the minor
number, any other commit the patch number, `type!:` the major number - so every
push to master is a new version (1.12.3) and nothing is bumped by hand.
Docker images have no .git: docker-up.sh and the image build pass
STREAMFINDER_VERSION and STREAMFINDER_COMMIT; the Windows setup .exe ships a
VERSION file ("1.12.3 a6d1e11"). `python streamfinder/version.py` prints both.
"""

import os
import re
import subprocess

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_URL = "https://github.com/poruto/stream-finder-torrent"

_BREAKING = re.compile(r"^\w+(\([^)]*\))?!:")
_FEATURE = re.compile(r"^feat(\([^)]*\))?:")


def from_subjects(subjects: list[str]) -> str:
    """Version after these commit subjects, oldest first."""
    major, minor, patch = 1, 0, 0
    for subject in subjects:
        if _BREAKING.match(subject):
            major, minor, patch = major + 1, 0, 0
        elif _FEATURE.match(subject):
            minor, patch = minor + 1, 0
        else:
            patch += 1
    return f"{major}.{minor}.{patch}"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", BASE_DIR, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    ).stdout


def _from_git() -> tuple[str, str]:
    try:
        commit = _git("rev-parse", "--short=7", "HEAD").strip()
        if not commit:
            return "dev", ""
        # A shallow clone doesn't have the history to count.
        if _git("rev-parse", "--is-shallow-repository").strip() == "true":
            return "dev", commit
        return from_subjects(
            _git("log", "--reverse", "--format=%s").splitlines()
        ), commit
    except (OSError, subprocess.SubprocessError):
        return "dev", ""


def _read() -> tuple[str, str]:
    if os.getenv("STREAMFINDER_VERSION"):
        return os.environ["STREAMFINDER_VERSION"], os.getenv("STREAMFINDER_COMMIT", "")
    try:
        with open(os.path.join(BASE_DIR, "VERSION"), encoding="utf-8") as f:
            parts = f.read().split()
        if parts:
            return parts[0], parts[1] if len(parts) > 1 else ""
    except OSError:
        pass
    return _from_git()


VERSION, COMMIT = _read()

if __name__ == "__main__":
    print(VERSION, COMMIT)
