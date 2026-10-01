"""Versions and release notes (talos.releases, CHANGELOG.md, the release script)."""

import os
import re
import subprocess
import sys
from pathlib import Path

from starlette.testclient import TestClient

from talos import __version__, releases
from talos.config import Settings
from talos.web import app

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / ".agents" / "skills" / "talos-dev" / "scripts" / "release.py"


def test_the_version_is_the_same_in_the_code_the_project_and_the_newest_release():
    pyproject = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    assert __version__ == pyproject == releases.latest_release()
    assert releases.SEMVER.match(__version__)


def test_every_release_has_a_semver_version_a_date_and_known_kinds_of_change():
    rs = releases.parse(releases.PATH.read_text())
    assert rs[0]["version"] == "Unreleased"
    for r in rs[1:]:
        assert releases.SEMVER.match(r["version"]) and re.match(r"\d{4}-\d{2}-\d{2}$", r["date"] or ""), r["version"]
        assert set(r["changes"]) <= set(releases.KINDS) and any(r["changes"].values()), r["version"]
    versions = [tuple(map(int, r["version"].split("."))) for r in rs[1:]]
    assert versions == sorted(versions, reverse=True)


def test_a_bullet_may_run_over_several_lines():
    rs = releases.parse("## [1.0.0] - 2026-01-01\n\n### Added\n- One thing\n  that goes on.\n- Two.\n")
    assert rs[0]["changes"]["Added"] == ["One thing that goes on.", "Two."]


def test_the_web_app_serves_the_release_notes(conn, vault, database):
    c = TestClient(app.create(Settings(home=vault.root.parent, dsn=database), allowed_hosts=["testserver"]))
    d = c.get("/api/release-notes").json()
    assert d["version"] == __version__ and d["releases"][0]["version"] in (__version__, "Unreleased")


def test_the_release_script_moves_unreleased_under_the_new_version_and_tags_it(tmp_path):
    (tmp_path / "src" / "talos").mkdir(parents=True)
    (tmp_path / "src" / "talos" / "__init__.py").write_text('__version__ = "0.6.0"\n')
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "talos"\nversion = "0.6.0"\n')
    (tmp_path / "CHANGELOG.md").write_text(
        "# Release notes\n\n## [Unreleased]\n\n### Added\n- Filters in the sidebar.\n\n## [0.6.0] - 2026-09-27\n\n"
        "### Added\n- Work space.\n\n[Unreleased]: https://example.com/r/compare/v0.6.0...HEAD\n")
    git = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True, text=True)
    git("init", "-q"); git("config", "user.email", "t@example.com"); git("config", "user.name", "t")
    git("add", "."); git("commit", "-q", "-m", "start")
    env = {**os.environ, "TALOS_RELEASE_ROOT": str(tmp_path)}
    r = subprocess.run([sys.executable, str(RELEASE), "minor"], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    text = (tmp_path / "CHANGELOG.md").read_text()
    rs = releases.parse(text)
    assert [x["version"] for x in rs] == ["Unreleased", "0.7.0", "0.6.0"]
    assert rs[0]["changes"] == {} and rs[1]["changes"] == {"Added": ["Filters in the sidebar."]}
    assert "[0.7.0]: https://example.com/r/compare/v0.6.0...v0.7.0" in text
    assert '__version__ = "0.7.0"' in (tmp_path / "src" / "talos" / "__init__.py").read_text()
    assert "v0.7.0" in git("tag").stdout
    again = subprocess.run([sys.executable, str(RELEASE), "patch"], env=env, capture_output=True, text=True)
    assert again.returncode != 0 and "nothing under [Unreleased]" in again.stderr
