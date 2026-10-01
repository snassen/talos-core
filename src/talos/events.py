"""Machine mail becomes events: backup reports, alarms and alerts as rows, not objects.

An extractor is data: which messages it reads (rule-style conditions), what kind of
event they are, where the reporting system's name comes from, and an ordered list
of status patterns. The first pattern that matches the subject or the start of the
body decides the status. Extractors are versioned: bumping a version re-reads every
matching message on the next run, so improving one re-reads history.

The defaults below are deliberately generic. They are meant to be tuned against the
real Backup, Alarm and 365 Alerts folders once those are synced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import psycopg
from psycopg.types.json import Jsonb

from talos import rules

STATUS_WORDS = [
    ("failed", r"\b(fail(ed|ure)?|errors?|fel|misslyckad(es)?|misslyckades|critical|kritisk|aborted|avbruten)\b"),
    ("warning", r"\b(warn(ings?)?|varning(ar)?|statistikvarning(ar)?|partial(ly)?|delvis)\b"),
    ("ok", r"\b(succe(ss|eded|ssful)|completed|slutförts|slutfördes|klar|lyckad(es)?|ok|resolved|åtgärdad)\b"),
]
# "Failed: 0", "0 errors", "Warnings = 0": a count of nothing is not a status.
_ZERO_COUNTS = re.compile(r"\b(fail(ed|ures?)?|errors?|warn(ings?)?|fel|varningar)\s*[:=]\s*0\b"
                          r"|\b0\s+(fail(ed|ures?)?|errors?|warn(ings?)?|fel|varningar)\b", re.I)
# Tags that reporting systems put first in the subject, e.g. Veeam's "[Success] Job name".
_TAG = re.compile(r"\[(success|succeeded|ok|warning|warnings|failed|failure|error|errors)\]", re.I)
# Veeam ONE: "... has been changed to Reset/resolved (previous state: Error)" — the new state counts.
_CHANGED_TO = re.compile(r"changed to ([a-z/]+)", re.I)
_TAG_STATUS = {"success": "ok", "succeeded": "ok", "ok": "ok", "warning": "warning", "warnings": "warning",
               "failed": "failed", "failure": "failed", "error": "failed", "errors": "failed",
               "reset/resolved": "ok", "resolved": "ok", "normal": "ok"}


@dataclass
class Extractor:
    name: str
    kind: str
    conditions: list[dict]  # ANDed
    version: int = 1
    alternatives: list[list[dict]] = field(default_factory=list)  # each ANDed; any one matching also counts
    system_from: str = "from_name"  # from_name | from_domain | subject:<regex with one group>
    statuses: list[tuple[str, str]] = field(default_factory=lambda: list(STATUS_WORDS))
    default_status: str = "info"

    def status(self, subject: str, body: str = "") -> str:
        """The subject decides when it can; the body only when the subject says nothing.

        Order within a text: an explicit tag ("[Success]"), then a stated new state
        ("changed to Error"), then status words, most serious first, after removing
        zero counts such as "Failed: 0" that report summaries are full of.
        """
        for text in (subject or "", body or ""):
            text = _ZERO_COUNTS.sub(" ", text)
            tag = _TAG.search(text)
            if tag:
                return _TAG_STATUS[tag.group(1).lower()]
            changed = _CHANGED_TO.search(text)
            if changed and changed.group(1).lower() in _TAG_STATUS:
                return _TAG_STATUS[changed.group(1).lower()]
            if changed and changed.group(1).lower() == "warning":
                return "warning"
            for status, pattern in self.statuses:
                if re.search(pattern, text, re.I):
                    return status
        return self.default_status

    def system(self, row: dict) -> str | None:
        if self.system_from == "from_domain":
            return (row["from_address"] or "").rpartition("@")[2] or None
        if self.system_from.startswith("subject:"):
            m = re.search(self.system_from.split(":", 1)[1], row["subject"] or "", re.I)
            return m.group(1) if m else None
        return row["from_name"] or row["from_address"]


# The work account's server rules already file machine mail into these folders; the folder is the
# strongest signal, the subject a fallback for mail that arrives elsewhere.
DEFAULTS = [
    Extractor("backup", "backup", [
        {"field": "is_automated", "op": "is_true"},
        {"field": "subject", "op": "matches", "value": r"backup|säkerhetskopi|veeam|acronis|synology|hyper backup"}],
        version=3, alternatives=[[{"field": "folder", "op": "matches", "value": r"(^|/)backup$"}]]),
    Extractor("alarm", "alarm", [
        {"field": "subject", "op": "matches", "value": r"\m(alarm|larm)"}],
        version=3, alternatives=[[{"field": "folder", "op": "matches", "value": r"(^|/)(alarm|alarms|larm)$"}]]),
    Extractor("m365-alert", "alert", [
        {"field": "from_domain", "op": "ends_with", "value": "microsoft.com"},
        {"field": "subject", "op": "matches", "value": r"alert|avisering|incident|risky|riskfyll"}],
        system_from="from_name", version=3,
        alternatives=[[{"field": "folder", "op": "matches", "value": r"(^|/)(365 alerts|important alerts)$"}]]),
]


def run(conn: psycopg.Connection, extractors: list[Extractor] | None = None) -> dict[str, int]:
    """Create events for matching messages that do not yet have one from this extractor version."""
    counts = {}
    for ex in extractors or DEFAULTS:
        where, params = rules.compile_conditions(ex.conditions)
        for alt in ex.alternatives:
            w, p = rules.compile_conditions(alt)
            where, params = f"({where}) or ({w})", params + p
        with conn.transaction():
            conn.execute("delete from event where extractor = %s and extractor_version <> %s", (ex.name, ex.version))
            rows = conn.execute(
                f"select m.id, m.subject, m.from_name, m.from_address, m.received_at, left(t.body_text, 2000) body"
                f" from message m left join message_text t on t.message_id = m.id"
                f" where ({where}) and not exists (select 1 from event e where e.message_id = m.id"
                f" and e.extractor = %s)", [*params, ex.name]).fetchall()
            for r in rows:
                eid = conn.execute("insert into entity (kind) values ('event') returning id").fetchone()["id"]

                conn.execute(
                    "insert into event (id, message_id, extractor, extractor_version, system, kind, status,"
                    " occurred_at, fields) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (eid, r["id"], ex.name, ex.version, ex.system(r), ex.kind, ex.status(r["subject"], r["body"]),
                     r["received_at"],
                     Jsonb({"subject": r["subject"]})))
                conn.execute("insert into edge (src, rel, dst, source) values (%s, 'reported_by', %s, %s)"
                             " on conflict do nothing", (eid, r["id"], f"extractor:{ex.name}@{ex.version}"))
            counts[ex.name] = len(rows)
    return counts
