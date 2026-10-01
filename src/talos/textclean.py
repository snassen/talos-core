"""Turning mail bodies into clean text. Deterministic and dependency-light.

Three jobs: HTML to readable text, removing quoted replies (so a thread's text is
not counted once per reply), and a short snippet. Reply markers cover English and
Swedish clients, since most of this mailbox is one or the other.
"""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser

_BLOCK_BREAK = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t ​‌‍﻿]+")

# A line that introduces the quoted original. Checked against a line, or a line
# joined with the next when a client wraps the attribution.
_ATTRIBUTION = [
    re.compile(r"^on .{3,250} wrote:\s*$", re.I),
    re.compile(r"^den .{3,250} skrev .{0,250}:\s*$", re.I),
    re.compile(r"^\d{1,2} .{3,60} skrev .{0,250}:\s*$", re.I),
    re.compile(r"^am .{3,250} schrieb .{0,250}:\s*$", re.I),
    re.compile(r"^-{2,}\s*(original message|ursprungligt meddelande|forwarded message|"
               r"vidarebefordrat meddelande|originalmeddelande)\s*-{2,}", re.I),
    re.compile(r"^_{8,}\s*$"),
]
_OUTLOOK_FROM = re.compile(r"^(from|från|fra):\s*\S", re.I)
_OUTLOOK_NEXT = re.compile(r"^(sent|skickat|date|datum|to|till):\s*\S", re.I)

_SUBJECT_PREFIX = re.compile(r"^\s*((re|sv|fw|fwd|vb|vs|aw|wg|antw|ang|tr)\s*(\[\d+\])?\s*:\s*)+", re.I)


def html_to_text(html: str) -> str:
    if not html:
        return ""
    # selectolax mangles the text next to a NUL ("a\x00b" came out as "abb"), so drop it first.
    tree = HTMLParser(html.replace("\x00", ""))
    for node in tree.css("script, style, head, title, noscript"):
        node.decompose()
    for node in tree.css("br"):
        node.replace_with("\n")
    root = tree.body or tree.root
    if root is None:
        return ""
    return tidy(root.text(separator="\n", strip=False))


def tidy(text: str) -> str:
    lines = [_SPACES.sub(" ", line).strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out = "\n".join(lines)
    return _BLOCK_BREAK.sub("\n\n", out).strip()


def strip_quotes(text: str) -> str:
    """The new part of a reply: everything before the first quote marker."""
    lines = text.split("\n")
    cut = len(lines)
    for i, line in enumerate(lines):
        s = line.strip()
        joined = (s + " " + lines[i + 1].strip()) if i + 1 < len(lines) else s
        if any(p.match(s) or p.match(joined) for p in _ATTRIBUTION):
            cut = i
            break
        if _OUTLOOK_FROM.match(s) and any(_OUTLOOK_NEXT.match(n.strip()) for n in lines[i + 1:i + 5]):
            cut = i
            break
    kept = [ln for ln in lines[:cut] if not ln.lstrip().startswith(">")]
    result = tidy("\n".join(kept))
    return result or tidy(text)


def snippet(text: str, limit: int = 200) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def normalize_subject(subject: str | None) -> str:
    return _SUBJECT_PREFIX.sub("", subject or "").strip().lower()
