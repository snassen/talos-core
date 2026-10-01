#!/usr/bin/env python3
"""Cut a release: `release.py 0.7.0` (or `release.py minor` / `major` / `patch`).

Moves everything under `## [Unreleased]` in CHANGELOG.md to a new `## [X.Y.Z] - today`, puts an empty
Unreleased back on top, updates the compare links, sets the version in src/talos/__init__.py and
pyproject.toml, then commits those three files and tags vX.Y.Z. Refuses when Unreleased is empty, the
version isn't higher, or the working tree has other staged changes. Push afterwards with
`git push && git push --tags`.
"""

import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(os.environ.get("TALOS_RELEASE_ROOT") or Path(__file__).resolve().parents[4])  # the env var: tests
CHANGELOG, INIT, PYPROJECT = ROOT / "CHANGELOG.md", ROOT / "src/talos/__init__.py", ROOT / "pyproject.toml"


def die(msg: str) -> None:
    sys.exit(f"release: {msg}")


def current() -> tuple[int, int, int]:
    m = re.search(r'__version__ = "(\d+)\.(\d+)\.(\d+)"', INIT.read_text())
    return tuple(int(x) for x in m.groups())


def main() -> None:
    if len(sys.argv) != 2:
        die("usage: release.py X.Y.Z | major | minor | patch")
    major, minor, patch = current()
    arg = sys.argv[1]
    new = {"major": f"{major + 1}.0.0", "minor": f"{major}.{minor + 1}.0", "patch": f"{major}.{minor}.{patch + 1}"}.get(arg, arg)
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", new):
        die(f"{new!r} is not a version (MAJOR.MINOR.PATCH)")
    if tuple(int(x) for x in new.split(".")) <= (major, minor, patch):
        die(f"{new} is not higher than {major}.{minor}.{patch}")
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    if staged:
        die("commit or unstage your other changes first: " + ", ".join(staged))
    text = CHANGELOG.read_text()
    m = re.search(r"^## \[Unreleased\]\s*\n(.*?)(?=^## \[)", text, re.S | re.M)
    if not m or not re.search(r"^- ", m.group(1), re.M):
        die("nothing under [Unreleased] in CHANGELOG.md: write what changed first")
    body = m.group(1).strip()
    old = f"{major}.{minor}.{patch}"
    text = text[:m.start()] + f"## [Unreleased]\n\n## [{new}] - {date.today().isoformat()}\n\n{body}\n\n" + text[m.end():]
    text = re.sub(r"^\[Unreleased\]: (.*)/compare/v[\d.]+\.\.\.HEAD$",
                  lambda x: f"[Unreleased]: {x.group(1)}/compare/v{new}...HEAD\n[{new}]: {x.group(1)}/compare/v{old}...v{new}",
                  text, count=1, flags=re.M)
    CHANGELOG.write_text(text)
    INIT.write_text(re.sub(r'__version__ = "[\d.]+"', f'__version__ = "{new}"', INIT.read_text()))
    PYPROJECT.write_text(re.sub(r'^version = "[\d.]+"', f'version = "{new}"', PYPROJECT.read_text(), count=1, flags=re.M))
    run = lambda *a: subprocess.run(a, cwd=ROOT, check=True)
    run("git", "add", str(CHANGELOG), str(INIT), str(PYPROJECT))
    run("git", "commit", "-q", "-m", f"Release v{new}")
    run("git", "tag", "-a", f"v{new}", "-m", f"Talos v{new} (see CHANGELOG.md)")
    print(f"Released v{new}. Push with: git push && git push --tags")


if __name__ == "__main__":
    main()
