"""The live manual (talos.manual, docs/manual.md): the ⓘ buttons in app.js and the manual's sections name the same
things, and the file reads into the blocks the dialog draws."""

import re
from pathlib import Path

from starlette.testclient import TestClient

from talos import manual
from talos.config import Settings
from talos.web import app

APP = (Path(__file__).resolve().parent.parent / "src" / "talos" / "web" / "static" / "app.js").read_text(encoding="utf-8")


def help_ids() -> set[str]:
    return set(re.findall(r"(?:helpBtn|withHelp)\([^()]*?'([a-z0-9-]+)'\)", APP))


def test_every_help_button_opens_a_section_of_the_manual():
    missing = help_ids() - set(manual.sections())
    assert help_ids() and not missing, f"app.js asks the manual for {sorted(missing)}, which docs/manual.md lacks"


def test_every_section_of_the_manual_has_a_button_somewhere():
    unused = set(manual.sections()) - help_ids()
    assert not unused, f"docs/manual.md has sections no ⓘ opens: {sorted(unused)}"


def test_every_see_also_names_a_section_and_every_section_has_text():
    secs = manual.sections()
    for sid, sec in secs.items():
        assert sec["blocks"], sid
        assert set(sec["see"]) <= set(secs) - {sid}, f"{sid} sees {sec['see']}"


def test_a_section_reads_into_paragraphs_lists_steps_tips_and_subheadings():
    secs = manual.parse(
        "# Preface, left out\n\nFor editors.\n\n"
        "## Watchers {#watchers}\nGuide: chapter 7\n\nA watcher keeps\nlooking.\n\n"
        "- one\n- two that\n  goes on\n\n### Make one\n1. Watch\n2. Add\n\n> **Tip.** Pause it\n> when bored.\n\n"
        "See also: mail\n\n## Mail {#mail}\nText.\n")
    w = secs["watchers"]
    assert list(secs) == ["watchers", "mail"]
    assert (w["title"], w["guide"], w["see"]) == ("Watchers", "chapter 7", ["mail"])
    assert w["blocks"] == [
        {"kind": "p", "text": "A watcher keeps looking."},
        {"kind": "ul", "items": ["one", "two that goes on"]},
        {"kind": "h", "text": "Make one"},
        {"kind": "ol", "items": ["Watch", "Add"]},
        {"kind": "tip", "text": "**Tip.** Pause it when bored."}]


def test_a_paragraph_straight_after_a_list_starts_a_new_block():
    b = manual.parse("## X {#x}\nThree ways:\n- a\n- b\nAfter.\n")["x"]["blocks"]
    assert [x["kind"] for x in b] == ["p", "ul", "p"]


def test_two_sections_with_one_id_is_an_error():
    try:
        manual.parse("## A {#a}\nx\n\n## B {#a}\ny\n")
    except ValueError as e:
        assert "a" in str(e)
    else:
        raise AssertionError("a repeated id was accepted")


def test_the_web_app_serves_the_manual(conn, vault, database):
    c = TestClient(app.create(Settings(home=vault.root.parent, dsn=database), allowed_hosts=["testserver"]))
    d = c.get("/api/manual").json()
    assert d["mail"]["title"] == "Mail" and d["mail"]["blocks"]
