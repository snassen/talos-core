"""The model step: compact case records out, validated decisions back in.

This follows the Codex benchmark's findings. Deterministic code prepares one record
per thread (IDs, dates, senders, subject, current values, and up to 1,800
characters of cleaned text). A model such as Jev answers in JSONL. Talos validates
the answer: every ID must be one it exported, every value must be allowed, and holds
(likely spam, unresolved) stay holds. Only then are the decisions stored, as
*proposed* assignments on the thread, with the run as their source.

Nothing a model says becomes active until accept() is called under a policy the
owner chooses. Nothing reaches a mailbox except through a changeset the owner commits.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from talos import personal, rules

BODY_CHARS = 1800
HOLDS = {"spam", "unresolved"}


class CaseError(ValueError):
    pass


def export(conn: psycopg.Connection, path: Path, *, model: str, purpose: str, dimension: str,
           conditions: list[dict] | None = None, thread_ids: list[int] | None = None,
           body_chars: int = BODY_CHARS, limit: int | None = None) -> str:
    """Write case records for threads to JSONL and register a model run. Returns the run id."""
    if thread_ids is None:
        where, params = rules.compile_conditions(conditions or [{"field": "medium", "op": "is", "value": "email"}])
        thread_ids = [r["thread_id"] for r in conn.execute(
            f"select distinct m.thread_id from message m where ({where}) and m.thread_id is not null"
            f" order by m.thread_id", params)]
    if limit is not None:
        thread_ids = thread_ids[:limit]
    records = []
    for tid in thread_ids:
        msgs = conn.execute(
            "select m.id, m.received_at, m.from_name, m.from_address, m.subject, m.direction,"
            " coalesce(t.quote_stripped, '') text from message m left join message_text t on t.message_id = m.id"
            " where m.thread_id = %s order by m.received_at nulls first, m.id", (tid,)).fetchall()
        if not msgs:
            continue
        current = {r["dimension_id"]: r["value"] for r in conn.execute(
            "select dimension_id, value from effective_assignment where entity_id = %s", (tid,))}
        text = "\n---\n".join(m["text"] for m in msgs if m["text"])
        records.append({
            "case_id": tid,
            "date": msgs[-1]["received_at"].date().isoformat() if msgs[-1]["received_at"] else None,
            "message_count": len(msgs),
            "senders": sorted({f"{m['from_name'] or ''} <{m['from_address']}>".strip() for m in msgs
                               if m["from_address"]}),
            "subject": msgs[0]["subject"],
            "includes_my_reply": any(m["direction"] == "out" for m in msgs),
            "current": current,
            "body_excerpt": text[:body_chars],
        })
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    path.write_text(data, encoding="utf-8")
    digest = hashlib.sha256(data.encode()).hexdigest()
    run_id = f"{model}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{digest[:8]}"
    conn.execute(
        "insert into model_run (id, model, purpose, params, input_count) values (%s, %s, %s, %s, %s)",
        (run_id, model, purpose, Jsonb({"dimension": dimension, "file": str(path), "sha256": digest,
                                        "case_ids": [r["case_id"] for r in records], "body_chars": body_chars}),
         len(records)))
    return run_id


def import_decisions(conn: psycopg.Connection, run_id: str, path: Path) -> dict:
    """Validate a decisions file against its run, then store the decisions as proposals.

    Each line: {"case_id": 123, "value": "Orders", "confidence": 0.8, "evidence": "..."}
    or a hold: {"case_id": 124, "hold": "spam"}. The whole file is refused on any
    unknown ID, duplicate ID or disallowed value, so a bad batch never half-lands.
    """
    run = conn.execute("select * from model_run where id = %s", (run_id,)).fetchone()
    if not run:
        raise CaseError(f"no model run {run_id!r}")
    dimension = run["params"]["dimension"]
    expected = set(run["params"]["case_ids"])
    allowed = conn.execute("select allowed from dimension where id = %s", (dimension,)).fetchone()["allowed"]
    decisions, seen = [], set()
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CaseError(f"line {n}: not JSON ({exc})") from None
        cid = d.get("case_id")
        if cid not in expected:
            raise CaseError(f"line {n}: case {cid!r} was not in run {run_id}")
        if cid in seen:
            raise CaseError(f"line {n}: case {cid} answered twice")
        seen.add(cid)
        if d.get("hold"):
            if d["hold"] not in HOLDS:
                raise CaseError(f"line {n}: unknown hold {d['hold']!r}")
        elif not d.get("value"):
            raise CaseError(f"line {n}: neither a value nor a hold")
        elif allowed and d["value"] not in allowed:
            raise CaseError(f"line {n}: {d['value']!r} is not an allowed {dimension}")
        decisions.append(d)

    proposed = held = 0
    with conn.transaction():
        for d in decisions:
            if d.get("hold"):
                held += 1
                conn.execute(
                    "insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, status,"
                    " evidence) values (%s, 'tag', %s, 'model', %s, 'proposed', %s)",
                    (d["case_id"], f"hold:{d['hold']}", run_id, Jsonb({"evidence": d.get("evidence")})))
                continue
            proposed += 1
            conn.execute(
                "insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, status, confidence,"
                " evidence) values (%s, %s, %s, 'model', %s, 'proposed', %s, %s)",
                (d["case_id"], dimension, d["value"], run_id, d.get("confidence"),
                 Jsonb({"evidence": d.get("evidence")})))
        conn.execute("update model_run set output_count = %s where id = %s", (len(decisions), run_id))
    return {"proposed": proposed, "held": held, "unanswered": len(expected - seen)}


def accept(conn: psycopg.Connection, run_id: str, *, min_confidence: float = 0.0, who: str = personal.OWNER_ID) -> int:
    """Make a run's proposals active: the owner's approval, recorded as decided_by."""
    cur = conn.execute(
        "update assignment set status = 'active', decided_by = %s, decided_at = now()"
        " where source_ref = %s and source_kind = 'model' and status = 'proposed'"
        " and value not like 'hold:%%' and coalesce(confidence, 0) >= %s", (who, run_id, min_confidence))
    return cur.rowcount


def reject(conn: psycopg.Connection, run_id: str, *, who: str = personal.OWNER_ID) -> int:
    cur = conn.execute(
        "update assignment set status = 'rejected', decided_by = %s, decided_at = now()"
        " where source_ref = %s and source_kind = 'model' and status = 'proposed'", (who, run_id))
    return cur.rowcount
