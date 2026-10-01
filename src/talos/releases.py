"""Release notes: CHANGELOG.md read into data, for the Release notes page and `talos --version`.

The file follows Keep a Changelog: `## [version] - date` headings (and `## [Unreleased]`), each with
`### Added / Changed / Deprecated / Removed / Fixed / Security` sections of `- ` bullets, which may
run over several lines. Versions follow Semantic Versioning. talos.__version__, pyproject.toml and
the newest release here must agree (tests/test_releases.py).
"""

from __future__ import annotations

import re
from pathlib import Path

from talos import __version__

PATH = Path(__file__).resolve().parents[2] / "CHANGELOG.md"
KINDS = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
_RELEASE = re.compile(r"^## \[(?P<version>[^\]]+)\](?:\s*-\s*(?P<date>\d{4}-\d{2}-\d{2}))?\s*$")
_SECTION = re.compile(r"^### (?P<kind>\w+)\s*$")
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def parse(text: str) -> list[dict]:
    """[{version, date, changes: {kind: [text, …]}}], newest first, as the file lists them."""
    releases: list[dict] = []
    cur, kind, item = None, None, None
    for line in text.splitlines():
        m = _RELEASE.match(line)
        if m:
            cur = {"version": m["version"], "date": m["date"], "changes": {}}
            releases.append(cur)
            kind = item = None
            continue
        if cur is None:
            continue
        m = _SECTION.match(line)
        if m:
            kind = m["kind"]
            cur["changes"].setdefault(kind, [])
            item = None
            continue
        if line.startswith("[") and "]: " in line:  # the link references at the end
            continue
        if kind and line.startswith("- "):
            cur["changes"][kind].append(line[2:].strip())
            item = len(cur["changes"][kind]) - 1
        elif kind and item is not None and line.startswith("  ") and line.strip():
            cur["changes"][kind][item] += " " + line.strip()
    return releases


def notes(path: Path = PATH) -> dict:
    """The page's data: this version, and every release (Unreleased first when it has changes)."""
    try:
        releases = parse(path.read_text(encoding="utf-8"))
    except OSError:
        releases = []
    releases = [r for r in releases if r["version"] != "Unreleased" or any(r["changes"].values())]
    return {"version": __version__, "releases": releases}


def latest_release(path: Path = PATH) -> str | None:
    return next((r["version"] for r in parse(path.read_text(encoding="utf-8")) if r["version"] != "Unreleased"), None)
