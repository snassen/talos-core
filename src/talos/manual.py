"""The live manual: docs/manual.md read into data, for the ⓘ beside the things it describes in Talos Web.

The file is the field guide's descriptions of places and controls, kept true to the pages as they are. A section
is `## Title {#id}`; under it an optional `Guide: …` line, then blocks separated by blank lines: paragraphs,
`### subheadings`, `- ` lists, `1. ` numbered steps and `> ` tips, and an optional `See also: id, id` line.
Inline markup (bold, italic, backticks) is left as text for the page's mdInline to draw, so nothing here
becomes markup. tests/test_manual.py holds app.js and the file to the same set of ids.
"""

from __future__ import annotations

import re
from pathlib import Path

PATH = Path(__file__).resolve().parents[2] / "docs" / "manual.md"
_SECTION = re.compile(r"^## (?P<title>.+?)\s*\{#(?P<id>[a-z0-9-]+)\}\s*$")
_ORDERED = re.compile(r"^\d+\.\s+")


def _blocks(lines: list[str]) -> list[dict]:
    """A section's body as [{kind: p | h | ul | ol | tip, text | items}], in order. A blank line ends a
    paragraph, a list or a tip; an indented line goes on with the bullet above it."""
    blocks: list[dict] = []
    cur: dict | None = None  # the block the next line may continue
    for line in lines:
        s = line.strip()
        if not s:
            cur = None
        elif s.startswith("### "):
            blocks.append({"kind": "h", "text": s[4:].strip()})
            cur = None
        elif s.startswith("- ") or _ORDERED.match(s):
            kind = "ul" if s.startswith("- ") else "ol"
            item = s[2:].strip() if kind == "ul" else _ORDERED.sub("", s)
            if cur is None or cur["kind"] != kind:
                cur = {"kind": kind, "items": []}
                blocks.append(cur)
            cur["items"].append(item)
        elif s.startswith(">"):
            if cur is None or cur["kind"] != "tip":
                cur = {"kind": "tip", "text": ""}
                blocks.append(cur)
            cur["text"] = (cur["text"] + " " + s[1:].strip()).strip()
        elif cur is not None and cur["kind"] in ("ul", "ol") and line.startswith("  "):
            cur["items"][-1] += " " + s
        elif cur is not None and cur["kind"] == "p":
            cur["text"] += " " + s
        else:
            cur = {"kind": "p", "text": s}
            blocks.append(cur)
    return blocks


def parse(text: str) -> dict[str, dict]:
    """{id: {id, title, guide, blocks, see}}, in the file's order. Text before the first section is the file's
    own preface, for whoever edits it, and is left out."""
    sections: dict[str, dict] = {}
    cur: dict | None = None
    body: list[str] = []

    def close():
        if cur is not None:
            cur["blocks"] = _blocks(body)

    for line in text.splitlines():
        m = _SECTION.match(line)
        if m:
            close()
            if m["id"] in sections:
                raise ValueError(f"the manual has two sections called {m['id']}")
            cur = sections[m["id"]] = {"id": m["id"], "title": m["title"], "guide": None, "see": []}
            body = []
            continue
        if cur is None:
            continue
        if line.startswith("Guide: ") and not body:
            cur["guide"] = line[7:].strip()
        elif line.startswith("See also: "):
            cur["see"] = [x.strip() for x in line[10:].split(",") if x.strip()]
        else:
            body.append(line)
    close()
    return sections


def sections(path: Path = PATH) -> dict[str, dict]:
    try:
        return parse(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
