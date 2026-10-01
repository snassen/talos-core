"""No trace of the owner as an individual is in the repository (talos.personal holds them, outside it).

Every word of every text file the repository holds (tracked, and new files not yet ignored) is compared,
by hash, with the owner's names, machine, other systems, account ids, employer, customers and places, so
this file names none of them. A failure says which file; `git grep -n` for the word you just wrote finds
the line. What belongs to the owner goes in their personal part (docs/install.md), never in the code.
"""

import hashlib
import re
import subprocess

from talos import personal

# sha256(word)[:16] of each forbidden word, lowercased (matched in any case) or exactly (a word that is
# also plain English in lower case).
ANY_CASE = {
    "056f7fb1883fa21f", "1a9c97282fbbf5f1", "403ad5235763a4f3", "48634f0f2554ca83", "48f1e0c4ee4fd97d",
    "4ccce7a3250a2a38", "4dd68e2ab3a30973", "4e04e4adadc93b7e", "5163dadca1c421c5", "5667ef73dac709ef",
    "5826ad61b3950f90", "601f26b2cf2cc1d4", "650b9dea86a5b588", "66fde61094b9f381", "696d1a88ee908fa1",
    "6cf74479e191b528", "7e23c9a29c6398b1", "8c35d5016df87734", "8cfde6efdfc4ed5a", "9cad66723ea7e3c9",
    "aa475625981493dc", "c61989f22ef4eae4", "d40b8c427d4966f9", "e466fd3397038e52", "eb9927ead289afa1",
    "ecf5a728cc219cb5", "fb725e03fb758991",
}
EXACT = {"51f9b3e644ea5a1c"}
WORD = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9]+")
TEXT = (".py", ".js", ".json", ".md", ".sql", ".css", ".html", ".toml", ".sh", ".txt", ".webmanifest", ".cfg",
        ".yml", ".yaml", ".ini", ".lock")


def _h(word: str) -> str:
    return hashlib.sha256(word.encode()).hexdigest()[:16]


def _files() -> list[str]:
    out = subprocess.run(["git", "-C", str(personal.REPO), "ls-files", "--cached", "--others", "--exclude-standard"],
                         capture_output=True, text=True).stdout.split()
    return [f for f in out if f.endswith(TEXT) or "/" not in f and "." not in f]


def test_no_word_in_the_repository_is_a_trace_of_the_owner():
    files = _files()
    if not files:  # not a git checkout (an sdist): nothing to check
        return
    found = {}
    for f in files:
        p = personal.REPO / f
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for w in set(WORD.findall(text + " " + f)):
            if _h(w.lower()) in ANY_CASE or _h(w) in EXACT:
                found.setdefault(f, 0)
                found[f] += 1
    assert not found, f"a trace of the owner is back (words per file): {found}"


def test_the_guard_matches_words_in_any_case_and_exact_words_only_exactly(monkeypatch):
    monkeypatch.setattr(__import__(__name__), "ANY_CASE", {_h("frobnicate")})
    monkeypatch.setattr(__import__(__name__), "EXACT", {_h("Widget")})
    hit = lambda w: _h(w.lower()) in ANY_CASE or _h(w) in EXACT  # noqa: E731
    assert hit("Frobnicate") and hit("FROBNICATE") and hit("Widget") and not hit("widget")
