"""The Jev adapter: TypeSafe's hosted classifier, outside the core (docs/enrichment-plan.md §1, §4).

Everything that leaves the Mac for Jev is built here, and nothing else talks to TypeSafe. The
routing policy (probabilities → decisions) is not here but in talos.policy; runs, storage and
the evaluation are in talos.jev_gold.

**The API** (as the Codex client `gmail-labeler/lib/gmail_labeler/jev_client.rb` uses it): one
case per request, `POST https://api.typesafe.ai/v1/systemone` with a Bearer key and the body
`{"state": {...}, "model": "jev-1.13.0", "questions": {...}}`. A question is either

- `{"type": "choice", "instructions": "...", "criteria": {value: criterion, ...}}`, answered
  `{"type": "choice", "choice": v, "confidence": p, "probabilities": {value: p, ...}}`, or
- `{"type": "noul", "instructions": "a statement"}`, answered `{"type": "noul", "noul": s}`
  with s from 0 to 1.

The response is `{"model", "answers": {name: answer}, "usage": {"input_tokens", "output_tokens"}}`.
Input costs $0.042 per million tokens; output is free.

**The key** is the Keychain item `typesafe-api-key` (service `talos`), which the owner adds
themselves. It is read on the first request, never when the client is built (so a dry run reads
no secret), never from the environment, and never logged or put in an error message.

**Questions** come from the loaded taxonomy (the `dimension` table, as `talos taxonomy load`
wrote it from rules/taxonomy.json), from versioned templates (TEMPLATE_VERSION):

- a one-value field (origin, type, topic, value) is one `choice` question, named after the
  field; its criteria are the field's values, each with its one-line definition;
- templates v1 asked a many-value field (ask, route) as one `noul` statement per value, named
  `field:value`. The first evaluation showed that fails on precision: Jev said yes to most
  statements (ask about 25% precise, route about 35%, IT Operations and Company & Internal
  said yes to almost everything), because every statement is judged alone;
- templates v2 (the default) ask each as one `choice` with a `none` option, since a message
  almost always has one main ask and one main work category: `ask` (none, question, action,
  decision, my_commitment) plus one statement `ask:deadline`, and `route` (none plus the work
  categories). talos.policy routes them and applies the cross-field gates.

Templates v2 also ask a Teams window (answer-key unit `window`) its own set: origin is not
asked (it is `person`, fixed; it was right on every Teams item), type and value are asked over
the short lists that suit a chat (TEAMS_VALUES), and every choice starts with TEAMS_PREAMBLE.
`question_sets()` returns both sets, {"email": ..., "teams": ...}; the question version hashes
both. `build_questions(dims, template=1)` still builds the v1 questions (one set for both).

Who the owner is goes once into the state (`recipient`), not into every question: it is sent
with every case, so it is kept to a few lines. `question_version()` hashes the questions, the
template version and that text together, so two runs with the same version asked exactly the
same thing.

**Records** (`record()`), from a unit (an answer-key item, or a backfill case built from message
ids by gold.unit()), in two unit designs:

- `message`: the anchor message alone: medium and account, date, who sent it (and whether
  the owner did), to and cc, subject, attachment names, and the text, up to BODY_MAX (1,800)
  characters;
- `context`: the same, plus a compact `context`: the thread's other messages or the pattern's
  other samples, one line each, so that the whole record stays within RECORD_MAX (2,500)
  characters.

A Teams item is different: one line alone is not worth judging, so in *both* designs it is its
conversation window (enrich.TEAMS_WINDOW_GAP, the same rule the answer key and step A use):
the lines in order with their time and speaker, the anchor marked, at most TEAMS_WINDOW_CAP
(30) lines, plus the three lines before the window, within RECORD_MAX. The two designs
therefore differ only on email.

Every text is masked (talos.mask) before it is capped. Bump RECORD_VERSION when a record's
shape or content changes.
"""

from __future__ import annotations

import asyncio
import email.utils
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Iterable

import httpx
import psycopg

from talos import accounts, gold, mask, personal, secrets
from talos.enrich import TEAMS_WINDOW_CAP, TEAMS_WINDOW_GAP
from talos.policy import NONE

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"
KEY_NAME = "typesafe-api-key"
PRICE_PER_MTOK_INPUT = 0.042     # dollars per million input tokens; output is free
CONCURRENCY = 8
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_RETRIES = 4
MAX_DELAY = 60.0                 # the longest wait between tries, Retry-After included
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 529})

RECORD_VERSION = 1
BODY_MAX = 1800
RECORD_MAX = 2500
CONTEXT_MIN = 400                # the context unit always gets at least this much room
LINE_MAX = 280                   # one context line
PARTICIPANTS_MAX = 6
UNITS = ("message", "context")

# Estimated tokens per character of the request body: calibrated on the Codex runs (the same
# questions with and without an 1,800-character excerpt: 0.36 tokens per excerpt character, and
# 1,374 tokens for 3,895 characters of questions plus a metadata-only state). Good to ±15%;
# a real run reports the exact usage.
TOKENS_PER_CHAR = 0.345

# Who the owner is, sent once per case as state.recipient: their own text in TALOS_HOME/config/recipient.txt
# (talos.personal; it names their employer, customers and family, so it stays out of the repository).
# From the labelling guide's description of the owner, shortened: every word here is paid for on every case,
# and it is part of question_version(), so the file must keep its exact wording.
GENERIC_RECIPIENT = (
    "The owner of this mail archive: work mail (IT operations and the company's systems, vendors and "
    "customers) and personal mail (family, shopping, travel, streaming). Teams chats are work chats. "
    "In the questions, \"you\" means the owner.")
RECIPIENT = personal.text("recipient.txt", GENERIC_RECIPIENT)

# How a record names the account a message came through: each account's jev_label in accounts.json
# (talos.personal), else its id; a Teams message's account without one is "Teams". Part of every record.
ACCOUNTS = {a["id"]: a["jev_label"] for a in accounts.ACCOUNTS if a.get("jev_label")}
# Who the owner is in a record (talos.personal): their name on mail they sent, and the flag for it.
OWNER = personal.owner()

# The templates the questions are built from, per field. Part of question_version().
TEMPLATE_VERSION = 2
TEMPLATES = (1, 2)
ONE_INSTRUCTIONS = {
    "origin": personal.wording(
        "jev.origin", "{description} Mass mailings from companies are marketing even when signed by a person; a "
                      "salesperson writing to you one-to-one is a person. Teams messages from colleagues are person."),
    "type": "{description} Pick the most specific value. A cold sales mail is sales_outreach whatever its origin.",
    "topic": "{description} Work/ topics are for work mail, the others for personal mail.",
    "value": "{description} Only spam and junk are noise; marketing, newsletters, delivery updates, codes, routine "
             "notices and everyday chat are transient; receipts and invoices are record_financial.",
}
# v1 only: ask and route as one statement per value.
MANY_STATEMENTS = {
    "ask": personal.wording("jev.ask.statement", "This message expects something of you personally (never true of "
                                                 "mass mailings, newsletters or marketing): {label}. {description}"),
    "route": "This is work mail (not personal mail) that belongs to the work category {label}: {description}",
}

# v2: ask and route as one choice with none (talos.policy.NONE), and ask's side statement.
CHOICE_INSTRUCTIONS = {
    "ask": personal.wording(
        "jev.ask.choice", "What {this} asks of you personally, or what you promise in it. The ask must be aimed at "
                          "you personally: a person writing to you, or a notice about your own account or service "
                          "that requires you to act. Mass mailings, newsletters, marketing, cold sales, and notices "
                          "to many never ask: pick none. If there are several, pick the main ask."),
    "route": "The work category {this} belongs to, for finding it from a binder. Pick the ONE main work "
             "category. IT Operations only when it is really about running IT day to day. Pick none for "
             "personal mail, or when no work category clearly fits.",
}
NONE_CRITERIA = {
    "ask": personal.wording("jev.ask.none", "Nothing is asked of you personally and you promise nothing."),
    "route": "Not work mail, or no clear work category.",
}
SIDE_STATEMENTS = {"ask": {"deadline": personal.wording("jev.ask.deadline", "It carries a date by which you must act.")}}

# v2, Teams windows: origin is not asked but fixed; type and value over short lists that suit a chat.
TEAMS_ORIGIN = "person"
TEAMS_VALUES = {
    "type": ("conversation", "question", "request", "fyi", "announcement", "reply_thanks", "meeting_notes",
             "document", "invitation", "incident", "other"),
}  # value keeps its full list on Teams: the short list halved its accuracy on the answer key (56% → 26%)
TEAMS_PREAMBLE = ("This is a Teams chat window between colleagues at work: judge it as a whole conversation, "
                  "as seen from the anchor line marked →. ")
TEAMS_INSTRUCTIONS = {
    "type": "{description} Pick what the conversation mainly is.",
    "topic": "{description} Teams chats are work chats: pick a Work/ topic unless the chat is plainly personal.",
}
MEDIA = ("email", "teams")


class JevError(RuntimeError):
    pass


class JevAuthError(JevError):
    """The key was refused. Nothing else will work, so a run stops at once."""


# ---------------------------------------------------------------- questions

def taxonomy_from_db(conn: psycopg.Connection, fields=gold.FIELDS) -> dict[str, dict]:
    """The loaded taxonomy in taxonomy.read()'s shape: {field: {cardinality, description, values}}."""
    out = {}
    for r in conn.execute("select id, cardinality, description, allowed, value_meta from dimension"
                          " where id = any(%s)", (list(fields),)):
        if not r["allowed"]:
            raise JevError(f"{r['id']} has no value list; load the taxonomy first: talos taxonomy load")
        meta = r["value_meta"] or {}
        out[r["id"]] = {"cardinality": r["cardinality"], "description": r["description"] or "",
                        "values": [{"value": v, "label": (meta.get(v) or {}).get("label", v),
                                    "description": (meta.get(v) or {}).get("description", "")}
                                   for v in r["allowed"]]}
    missing = [f for f in fields if f not in out]
    if missing:
        raise JevError(f"no dimension for {', '.join(missing)}; load the taxonomy first: talos taxonomy load")
    return {f: out[f] for f in fields}


def _plain(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.casefold())


def _criterion(v: dict) -> str:
    """A value's definition, led by its label when the label says more than the id."""
    desc = (v.get("description") or "").strip()
    label = (v.get("label") or "").strip()
    if label and _plain(label) != _plain(v["value"]) and _plain(label) not in _plain(v["value"]):
        return f"{label}: {desc}" if desc else label
    return desc or label or v["value"]


def question_name(field: str, value: str | None = None) -> str:
    return field if value is None else f"{field}:{value}"


def _values(field: str, d: dict, teams: bool) -> list[dict]:
    """A field's values, restricted to TEAMS_VALUES for a Teams window, in that list's order."""
    if not (teams and field in TEAMS_VALUES):
        return d["values"]
    by = {v["value"]: v for v in d["values"]}
    missing = [v for v in TEAMS_VALUES[field] if v not in by]
    if missing:
        raise JevError(f"the Teams list of {field} has values the taxonomy lacks: {', '.join(missing)}")
    return [by[v] for v in TEAMS_VALUES[field]]


def _build_v1(dims: dict[str, dict]) -> dict[str, dict]:
    qs: dict[str, dict] = {}
    for f, d in dims.items():
        if d["cardinality"] == "one":
            tpl = ONE_INSTRUCTIONS.get(f, "{description}")
            qs[question_name(f)] = {"type": "choice",
                                    "instructions": tpl.format(description=(d.get("description") or "").strip()),
                                    "criteria": {v["value"]: _criterion(v) for v in d["values"]}}
        else:
            tpl = MANY_STATEMENTS.get(f, "{label}: {description}")
            for v in d["values"]:
                qs[question_name(f, v["value"])] = {
                    "type": "noul", "instructions": tpl.format(label=v.get("label") or v["value"],
                                                               description=(v.get("description") or "").strip())}
    return qs


def _build_v2(dims: dict[str, dict], teams: bool) -> dict[str, dict]:
    qs: dict[str, dict] = {}
    pre = TEAMS_PREAMBLE if teams else ""
    this = "this chat window" if teams else "this message"
    for f, d in dims.items():
        desc = (d.get("description") or "").strip()
        if f == "origin" and teams:
            continue  # fixed to TEAMS_ORIGIN, not asked
        if d["cardinality"] == "one":
            tpl = (TEAMS_INSTRUCTIONS if teams else ONE_INSTRUCTIONS).get(f, "{description}")
            qs[question_name(f)] = {"type": "choice", "instructions": pre + tpl.format(description=desc),
                                    "criteria": {v["value"]: _criterion(v) for v in _values(f, d, teams)}}
            continue
        side = SIDE_STATEMENTS.get(f, {})
        criteria = {NONE: NONE_CRITERIA.get(f, "None of these.")}
        criteria.update({v["value"]: _criterion(v) for v in d["values"] if v["value"] not in side})
        tpl = CHOICE_INSTRUCTIONS.get(f, "{description} Pick the main one, or none.")
        qs[question_name(f)] = {"type": "choice", "instructions": pre + tpl.format(this=this, description=desc),
                                "criteria": criteria}
        for v, statement in side.items():
            qs[question_name(f, v)] = {"type": "noul", "instructions": statement}
    return qs


def build_questions(dims: dict[str, dict], *, template: int = TEMPLATE_VERSION, teams: bool = False) -> dict[str, dict]:
    """The questions for every field, in the taxonomy's order (see the module docstring): the
    email set, or with teams the Teams-window set (templates v2; v1 has one set for both)."""
    if template not in TEMPLATES:
        raise JevError(f"question templates are one of {', '.join(map(str, TEMPLATES))}")
    return _build_v1(dims) if template == 1 else _build_v2(dims, teams)


def question_sets(dims: dict[str, dict], template: int = TEMPLATE_VERSION) -> dict[str, dict]:
    """{"email": questions, "teams": questions} for a run; v1 asks both the same."""
    return {m: build_questions(dims, template=template, teams=m == "teams") for m in MEDIA}


def questions_for(qsets: dict[str, dict], rec: dict) -> dict:
    """The question set for one record: a Teams window gets the Teams set."""
    return qsets["teams" if rec.get("unit") == "window" else "email"]


def question_version(questions: dict, recipient: str = RECIPIENT, *, template: int = TEMPLATE_VERSION) -> str:
    """A hash of exactly what is asked: the questions (v2: both sets), the template version and
    the recipient text. For v1 it is the hash v1 runs stored (one set, template 1)."""
    blob = json.dumps({"templates": template, "questions": questions, "recipient": recipient},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _choice(f: str, answers: dict, allowed: set[str], name: str | None = None) -> tuple[dict, dict]:
    name = name or question_name(f)
    a = answers.get(name)
    probs = a.get("probabilities") if isinstance(a, dict) else None
    if not isinstance(probs, dict) or not probs:
        raise JevError(f"invalid Jev response: no probabilities for {name}")
    try:
        scores = {v: float(p) for v, p in probs.items()}
    except (TypeError, ValueError):
        raise JevError(f"invalid Jev response: a probability of {name} is not a number") from None
    unknown = [v for v in scores if v not in allowed]
    if unknown:
        raise JevError(f"invalid Jev response: {name} has an unknown value {unknown[0]!r}")
    return scores, a


def _noul(name: str, answers: dict) -> tuple[float, dict]:
    a = answers.get(name)
    try:
        s = float(a["noul"])
    except (TypeError, KeyError, ValueError):
        raise JevError(f"invalid Jev response: no score for {name}") from None
    if not 0.0 <= s <= 1.0:
        raise JevError(f"invalid Jev response: the score for {name} is outside 0..1")
    return s, a


def read_answers(dims: dict[str, dict], answers: dict, *, template: int = TEMPLATE_VERSION,
                 teams: bool = False) -> dict[str, dict]:
    """Jev's answers per field: {field: {"many": bool, "scores": {value: p}, "raw": {...}}}.

    v2: ask and route are one choice; their scores are the choice's probabilities (`none`
    included) plus, under its own name, each side statement's score (ask: deadline); raw is
    {"choice": answer, statement: answer}. A Teams window's origin is not asked: its scores are
    {TEAMS_ORIGIN: 1.0} and its raw says it was fixed. Raises JevError when an answer is
    missing or malformed."""
    if not isinstance(answers, dict):
        raise JevError("invalid Jev response: answers is not an object")
    out = {}
    for f, d in dims.items():
        if template >= 2 and teams and f == "origin":
            out[f] = {"many": False, "scores": {TEAMS_ORIGIN: 1.0},
                      "raw": {"fixed": TEAMS_ORIGIN, "why": "a Teams window: origin is not asked"}}
        elif d["cardinality"] == "one":
            allowed = {v["value"] for v in (_values(f, d, teams) if template >= 2 else d["values"])}
            scores, a = _choice(f, answers, allowed)
            out[f] = {"many": False, "scores": scores, "raw": a}
        elif template >= 2:
            side = SIDE_STATEMENTS.get(f, {})
            allowed = {NONE} | {v["value"] for v in d["values"] if v["value"] not in side}
            scores, a = _choice(f, answers, allowed)
            raw = {"choice": a}
            for v in side:
                scores[v], raw[v] = _noul(question_name(f, v), answers)
            out[f] = {"many": True, "scores": scores, "raw": raw}
        else:
            scores, raw = {}, {}
            for v in d["values"]:
                scores[v["value"]], raw[v["value"]] = _noul(question_name(f, v["value"]), answers)
            out[f] = {"many": True, "scores": scores, "raw": raw}
    return out


# ---------------------------------------------------------------- records

def _collapse(text: str | None) -> str:
    text = (text or "").replace("\r", "")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _clean(text: str | None, cap: int) -> str:
    """Whitespace collapsed, masked, then capped (masking first: a masked URL leaves room for text)."""
    t = mask.mask(_collapse(text))
    return t if len(t) <= cap else t[: cap - 1].rstrip() + "…"


def _who(name: str | None, address: str | None) -> str:
    name = (name or "").strip()
    address = (address or "").strip()
    if name and address and name.casefold() != address.casefold():
        return mask.mask(f"{name} <{address}>")
    return mask.mask(name or address or "?")


def _when(dt) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M") if dt else None


def _text(m: dict) -> str:
    return m.get("text") or m.get("quote_stripped") or m.get("body_text") or ""


def _medium(m: dict) -> str:
    if m.get("medium") == "email":
        return "email"
    kind = {"oneOnOne": "one-to-one chat", "meeting": "meeting chat"}.get(m.get("chat_type") or "", "group chat")
    return f"Teams {kind}"


def _people(parts: list[dict], role: str) -> list[str]:
    ps = [_who(p.get("name"), p.get("address")) for p in parts if p.get("role") == role]
    return ps[:PARTICIPANTS_MAX] + ([f"… and {len(ps) - PARTICIPANTS_MAX} more"] if len(ps) > PARTICIPANTS_MAX else [])


def _message_state(m: dict) -> dict:
    s = {"medium": _medium(m),
         "account": ACCOUNTS.get(m.get("account_id"), m.get("account_id") if m.get("medium") == "email" else "Teams"),
         "date": _when(m.get("received_at")),
         OWNER["sent_by_key"]: m.get("direction") == "out",
         "from": _who(m.get("from_name"), m.get("from_address"))}
    parts = m.get("participants") or []
    for role in ("to", "cc"):
        ps = _people(parts, role)
        if ps:
            s[role] = ps
    if m.get("subject"):
        s["subject" if m.get("medium") == "email" else "chat"] = _clean(m["subject"], 300)
    atts = [a.get("filename") for a in (m.get("attachments") or []) if a.get("filename")]
    if atts:
        s["attachments"] = [_clean(a, 80) for a in atts[:5]] + ([f"… and {len(atts) - 5} more"] if len(atts) > 5 else [])
    return s


def _line(m: dict, *, anchor_subject: str | None, mark: str = "") -> str:
    who = OWNER["name"] if m.get("direction") == "out" else (m.get("from_name") or m.get("from_address") or "?")
    subj = m.get("subject") if m.get("medium") == "email" and m.get("subject") != anchor_subject else None
    body = _collapse(_text(m)).replace("\n", " / ")
    parts = [f"{mark}{_when(m.get('received_at')) or ''} {who}:".strip()]
    if subj:
        parts.append(f"[{subj}]")
    parts.append(body or "(no text)")
    return _clean(" ".join(parts), LINE_MAX)


def _fit(lines: list[str], order: list[int], budget: int) -> tuple[list[int], int]:
    """Which lines fit the budget, taken in order of preference; returned in their own order."""
    keep, used = [], 0
    for i in order:
        cost = len(lines[i]) + 3  # the JSON list's quotes and comma
        if used + cost > budget:
            continue
        keep.append(i)
        used += cost
    return sorted(keep), used


def _context(it: dict, budget: int) -> dict:
    """The compact context of an item for the context unit, within budget characters."""
    anchor = it["message"] or {}
    subj = anchor.get("subject")
    unit = it["unit"]
    if unit == "thread":
        msgs = [m for m in it["context"] if m["id"] != anchor.get("id")]
        pos = next((i for i, m in enumerate(it["context"]) if m["id"] == anchor.get("id")), len(it["context"]))
        before = [i for i in range(len(msgs)) if i < pos][::-1]   # nearest first: what it answers
        after = [i for i in range(len(msgs)) if i >= pos]
        order = [x for pair in zip(before, after) for x in pair] + before[len(after):] + after[len(before):]
        lines = [_line(m, anchor_subject=subj) for m in msgs]
        keep, _ = _fit(lines, order, budget - 80)
        total = it.get("thread_messages") or len(it["context"])
        out = {"kind": f"the rest of the thread ({total} messages in all)", "lines": [lines[i] for i in keep]}
        if len(keep) < total - 1:
            out["not_shown"] = total - 1 - len(keep)
        return out
    if unit == "pattern":
        p = it.get("pattern") or {}
        head = "other mail from the same sender with the same subject pattern"
        if p.get("message_count"):
            head += (f" ({p['message_count']} messages in {p.get('thread_count') or '?'} threads,"
                     f" {_when(p.get('first_at')) or '?'} to {_when(p.get('last_at')) or '?'})")
        lines = [_line(m, anchor_subject=None) for m in it["context"]]
        keep, _ = _fit(lines, list(range(len(lines))), budget - len(head) - 40)
        return {"kind": head, "lines": [lines[i] for i in keep]}
    return {"kind": "none: a single message"}


def _window(it: dict, budget: int) -> dict:
    """A Teams window: its lines in order, each with its time and speaker, the anchor marked →,
    at most TEAMS_WINDOW_CAP lines (nearest the anchor first when they do not all fit), and the
    lines just before the window (the answer key keeps three)."""
    anchor = it["message"] or {}
    msgs = it["context"] or [anchor]
    a = next((i for i, m in enumerate(msgs) if m["id"] == anchor.get("id")), 0)
    lines = [_line(m, anchor_subject=None, mark="→ " if i == a else "") for i, m in enumerate(msgs)]
    order = sorted(range(len(lines)), key=lambda i: (abs(i - a), i))[:TEAMS_WINDOW_CAP]
    kind = (f"a Teams conversation window (a new window starts after a gap of more than {TEAMS_WINDOW_GAP});"
            " → marks the line this case is anchored on; judge the window as a whole, as seen from that line")
    keep, used = _fit(lines, order, budget - len(kind) - 60)
    before = [_line(m, anchor_subject=None) for m in it.get("before") or []]
    bkeep, _ = _fit(before, list(range(len(before)))[::-1], budget - len(kind) - 60 - used)
    out = {"kind": kind, "lines": [lines[i] for i in keep]}
    if bkeep:
        out["before_window"] = [before[i] for i in bkeep]
    if len(keep) < len(lines):
        out["not_shown"] = len(lines) - len(keep)
    return out


def record(it: dict, unit: str) -> dict:
    """The case record (state without the recipient) for one unit, in the given unit design,
    masked and capped.

    `it` is a unit as gold.unit() loads it from message ids (its anchor `message`, `unit` kind,
    `context`, `before`, and `thread_messages` or `pattern`): an answer-key item (gold.item(),
    which is such a unit plus its labels) or an enrichment-backfill case (talos.backfill). The
    same messages give the same record either way; a test holds the answer key's records
    byte-identical.

    A Teams item (answer-key unit `window`) is always its whole window, in both designs: one
    Teams line alone is not worth judging. Its record is the same whichever unit is asked for,
    so the two designs differ only on email."""
    if unit not in UNITS:
        raise JevError(f"unit is one of {', '.join(UNITS)}")
    m = it["message"]
    if not m:
        raise JevError(f"item {it.get('position', it.get('key', '?'))} has no message")
    if it["unit"] == "window":
        rec = {"unit": "window", **_message_state(m)}
        rec["window"] = _window(it, RECORD_MAX - record_chars(rec) - 20)
        return rec
    rec = {"unit": unit, **_message_state(m), "text": _clean(_text(m), BODY_MAX)}
    if unit == "message":
        return rec
    size = record_chars(rec)
    budget = RECORD_MAX - size - 20
    if budget < CONTEXT_MIN:   # a long message: shorten its text to leave the context some room
        cut = CONTEXT_MIN - budget
        rec["text"] = _clean(_text(m), max(200, BODY_MAX - cut))
        budget = RECORD_MAX - record_chars(rec) - 20
    rec["context"] = _context(it, budget)
    return rec


def record_sha(rec: dict) -> str:
    """The hash of a record as it is sent (encode()): a stored case says which record it asked about."""
    return hashlib.sha256(encode(rec)).hexdigest()


def record_chars(rec: dict) -> int:
    """A record's size as sent: its compact JSON, in characters."""
    return len(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))


def request_body(rec: dict, questions: dict, *, model: str = MODEL) -> dict:
    return {"state": {"recipient": RECIPIENT, **rec}, "model": model, "questions": questions}


def encode(body: dict) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()


def estimate_tokens(body: dict) -> int:
    return round(len(encode(body).decode()) * TOKENS_PER_CHAR)


def cost(input_tokens: int, price: float = PRICE_PER_MTOK_INPUT) -> float:
    return input_tokens * price / 1_000_000


# ---------------------------------------------------------------- the client

@dataclass
class Usage:
    requests: int = 0
    retries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def as_dict(self) -> dict:
        return {"requests": self.requests, "retries": self.retries, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens}


@dataclass
class Result:
    case_id: object
    response: dict | None
    seconds: float
    error: str | None = None
    usage: dict = field(default_factory=dict)


def retry_after(value: str | None, now: float | None = None) -> float | None:
    """Seconds to wait from a Retry-After header: delta-seconds or an HTTP date; None if absent
    or unreadable."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = time.time() if now is None else now
    return max(0.0, when.timestamp() - now)


class JevClient:
    """Posts one case per request, CONCURRENCY in flight, and retries 429 and 5xx with backoff,
    honouring Retry-After up to MAX_DELAY. The key is read from the Keychain on the first
    request only. `usage` adds up requests, retries and tokens."""

    def __init__(self, *, model: str = MODEL, endpoint: str = ENDPOINT, concurrency: int = CONCURRENCY,
                 timeout: httpx.Timeout | float = TIMEOUT, max_retries: int = MAX_RETRIES,
                 max_delay: float = MAX_DELAY, transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], Awaitable] | None = None, key_name: str = KEY_NAME):
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        self.model, self.endpoint, self.concurrency = model, endpoint, concurrency
        self.timeout, self.max_retries, self.max_delay = timeout, max_retries, max_delay
        self._transport, self._sleep, self._key_name = transport, sleep or asyncio.sleep, key_name
        self._key: str | None = None
        self.usage = Usage()

    def __repr__(self) -> str:  # never the key
        return f"JevClient(model={self.model!r}, endpoint={self.endpoint!r}, concurrency={self.concurrency})"

    def _authorization(self) -> str:
        if self._key is None:
            self._key = secrets.get(self._key_name)
        return f"Bearer {self._key}"

    def backoff(self, attempt: int) -> float:
        return min(0.5 * (2 ** attempt), 8.0, self.max_delay)

    async def decide(self, http: httpx.AsyncClient, body: dict) -> dict:
        """One case: the parsed response, after retries. Raises JevError (JevAuthError on 401/403)."""
        content = encode(body)
        attempt = 0
        while True:
            headers = {"Authorization": self._authorization(), "Content-Type": "application/json"}
            try:
                self.usage.requests += 1
                resp = await http.post(self.endpoint, content=content, headers=headers)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise JevError(f"network failure after {attempt} retries: {type(exc).__name__}") from None
                await self._sleep(self.backoff(attempt))
                attempt += 1
                self.usage.retries += 1
                continue
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                wait = retry_after(resp.headers.get("retry-after"))
                await self._sleep(min(self.max_delay, wait if wait is not None else self.backoff(attempt)))
                attempt += 1
                self.usage.retries += 1
                continue
            if resp.status_code in (401, 403):
                raise JevAuthError(f"TypeSafe refused the key (HTTP {resp.status_code}); check the Keychain item"
                                   f" {self._key_name!r} (service {secrets.SERVICE!r})")
            if resp.status_code != 200:
                raise JevError(f"TypeSafe HTTP {resp.status_code}"
                               + (f" after {attempt} retries" if attempt else ""))
            try:
                data = resp.json()
            except ValueError:
                raise JevError("invalid Jev response: not JSON") from None
            if not isinstance(data, dict) or not all(k in data for k in ("model", "answers", "usage")):
                raise JevError("invalid Jev response: model, answers or usage missing")
            u = data.get("usage") or {}
            self.usage.input_tokens += int(u.get("input_tokens") or 0)
            self.usage.output_tokens += int(u.get("output_tokens") or 0)
            return data

    async def run(self, cases: Iterable[tuple[object, dict]], on_result: Callable[[Result], None]) -> None:
        """Send every (case_id, body), CONCURRENCY at a time; call on_result for each as it
        finishes (in the calling thread, so it may write to the database). A refused key stops
        the run: the cases not yet sent are not sent, and JevAuthError is raised."""
        cases = list(cases)
        if not cases:
            return
        sem = asyncio.Semaphore(self.concurrency)
        stop = asyncio.Event()
        auth_error: list[JevAuthError] = []
        async with httpx.AsyncClient(transport=self._transport, timeout=self.timeout,
                                     limits=httpx.Limits(max_connections=self.concurrency)) as http:
            async def one(case_id, body):
                async with sem:
                    if stop.is_set():
                        return
                    t0 = time.monotonic()
                    try:
                        resp = await self.decide(http, body)
                    except JevAuthError as exc:
                        stop.set()
                        auth_error.append(exc)
                        return
                    except JevError as exc:
                        on_result(Result(case_id, None, time.monotonic() - t0, str(exc)))
                        return
                    u = resp.get("usage") or {}
                    on_result(Result(case_id, resp, time.monotonic() - t0, None,
                                     {"input_tokens": int(u.get("input_tokens") or 0),
                                      "output_tokens": int(u.get("output_tokens") or 0)}))

            await asyncio.gather(*(one(c, b) for c, b in cases))
        if auth_error:
            raise auth_error[0]


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
