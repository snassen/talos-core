"""The talos-dev skill (.agents/skills/talos-dev) must keep describing the code as it is.

Claude Code and Codex both read it, and either may change the code, so it would slowly drift into
stale advice. These tests read every `code span` in the skill and check what can be checked: the
files it names exist, the functions and constants it names are still defined, the CSS tokens are in
style.css, the markers it quotes are in app.js, and its scripts are there to run. A test that fails
here means: update the skill in the same commit as the change that made it untrue.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / ".agents" / "skills" / "talos-dev"
DOCS = [SKILL / "SKILL.md", *sorted((SKILL / "references").glob("*.md"))]
SRC = "\n".join(p.read_text() for p in [*(ROOT / "src" / "talos").rglob("*.py"), *(ROOT / "tests").glob("*.py")])
APP = (ROOT / "src" / "talos" / "web" / "static" / "app.js").read_text()
CSS = (ROOT / "src" / "talos" / "web" / "static" / "style.css").read_text()
CODE = SRC + APP
# Files the skill names that live in the data folder (~/TalosData), not in the repo.
DATA_FILES = {"argus.json", "enrich.json", "web.json", "frames.py", "accounts.json", "structure.json",  # also config/
              "owner.json", "questions.json", "taxonomy.json", "argus-services.json"}
# Placeholders the skill uses in examples, and names from other tools (Playwright).
EXAMPLES = {"script.py", "viewX", "wait_for_timeout"}
EXTENSIONS = {"py", "js", "css", "json", "md", "sql", "sh", "html"}
SEARCH_DIRS = [ROOT, ROOT / "src" / "talos", ROOT / "src" / "talos" / "web", ROOT / "src" / "talos" / "web" / "static",
               ROOT / "docs", ROOT / "tests", ROOT / "scripts", SKILL, SKILL / "references", SKILL / "scripts"]


def spans() -> list[tuple[str, str]]:
    return [(p.name, s) for p in DOCS for s in re.findall(r"`([^`\n]+)`", p.read_text())]


def defined(name: str) -> bool:
    """A function, class, constant or variable of that name is defined somewhere in the code."""
    n = re.escape(name)
    return bool(re.search(rf"(?:def|class|function|const|let|var)\s+{n}\b|^\s*{n}\s*=|\b{n}\s*:\s*[\[{{(]", CODE, re.M))


def test_every_file_the_skill_names_exists():
    missing = []
    for doc, s in spans():
        s = re.sub(r"<[^>]*>\S*", "", s)  # <placeholder>/file.json is an example, not a file
        for path in re.findall(r"[\w./-]+\.(?:py|js|css|json|md|sql|sh|html)\b", s):
            name = path.split("/")[-1]
            if "*" in path or "NNN" in path or name in DATA_FILES | EXAMPLES or path.startswith(("~", "http")):
                continue
            if not any((d / path).exists() for d in SEARCH_DIRS):
                missing.append(f"{doc}: {path}")
    assert not missing, "the skill names files that are gone: " + ", ".join(missing)


def test_every_function_and_constant_the_skill_names_is_still_defined():
    missing = []
    for doc, s in spans():
        # module.function (Python): the module file defines it
        for mod, fn in re.findall(r"\b(\w+)\.(\w+)\(?", s):
            f = ROOT / "src" / "talos" / f"{mod}.py"
            if fn not in EXTENSIONS and f.exists() and not re.search(rf"^(?:def|class)\s+{fn}\b|^{fn}\s*=", f.read_text(), re.M):
                missing.append(f"{doc}: {mod}.{fn}")
        # a call at the start of the span: h(…), render(keepFocus), pollWhileRefreshing(d, path)
        m = re.match(r"(?:async function\s+)?([A-Za-z_]\w*)\(", s)
        if m and "." not in s.split("(")[0] and m.group(1) not in EXAMPLES | {"view", "if"} and not defined(m.group(1)):
            missing.append(f"{doc}: {m.group(1)}()")
        # CONSTANTS written alone
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", s) and not defined(s):
            missing.append(f"{doc}: {s}")
    assert not missing, "the skill names code that is gone: " + ", ".join(sorted(set(missing)))


def test_every_css_token_the_skill_names_is_in_the_stylesheet():
    tokens = {t for _, s in spans() for t in re.findall(r"^--[a-z][\w-]*$", s)} - {"--full"}
    missing = sorted(t for t in tokens if t.rstrip("-*") and not re.search(re.escape(t.rstrip("*")), CSS + APP))
    assert not missing, "the skill names CSS tokens that are gone: " + ", ".join(missing)


def test_the_markers_the_skill_quotes_are_in_the_code():
    for marker in ["// ---------------------------------------------------------------- the answer key",
                   "const RENDER = {", "function pollWhileRefreshing", "async function render(keepFocus)",
                   "local.talos.web", "local.talos.sync"]:
        assert marker in APP + SRC, f"the skill relies on {marker!r}, which is gone"


def test_the_skill_has_its_front_matter_and_its_scripts():
    head = (SKILL / "SKILL.md").read_text().split("---")[1]
    assert "name: talos-dev" in head and "description:" in head
    for script in ["verify.sh", "restart-web.sh"]:
        p = SKILL / "scripts" / script
        assert p.exists() and p.stat().st_mode & 0o111, f"{script} is missing or not executable"
