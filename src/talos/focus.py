"""Focused Jev runs: only the questions a case is unsure of, where the answer matters.

docs/enrichment-plan.md §11 (two-level acceptance). The backfill asks every field of
every case (about 5,900 input tokens a case). Once the boundaries are accepted, what is left is
a smaller set of cases where one field is unsure or contradicts a rule. A focused run asks that
field alone, of exactly those cases, for a fraction of the cost.

**The question.** `sender_kind` is a binary choice between the taxonomy's two definitions (a human
wrote this, to the owner or a group they are in, a person via a platform included; or a machine or a
company system produced it). `kind` is one choice over the eleven kinds with their definitions
(KIND_INSTRUCTIONS), so kind asked directly can be compared with kind derived from the fine type.
Any other field is asked its normal question (talos.jev, templates v2) alone: ask with its
deadline statement. A Teams window's origin is fixed (its sender_kind is people), so origin and
sender_kind never ask a window; kind asks it over the full list.

**Where** (`--where`), over the backfill's cases (the latest enrich-backfill run of each stage,
and every focused run since, the newest answer per case counting):

- `uncertain`: the field's probability is under its two-level threshold (a boundary's side under
  0.90; an exact value, ask or route under 0.70 or with a margin under 0.15);
- `disagree`: the case's answer contradicts the effective rule, pre-pass or human value of its
  anchor message (for sender_kind: its sender_kind, or else its origin's side);
- `all`: every case.

A unit is asked whole: when one of a template's three samples is chosen, all three are, so its
answer can reach the template's other messages. The cases are rebuilt from what the backfill
stored (unit, anchor, members, the samples as context), not selected again, so accepting values
(which moves mail between the machine and person pools) does not change them; the record sent is
the backfill's. `--stage` and `--since` narrow the units.

**Storage** is the backfill's: a `model_run` (purpose 'enrich-focus', params.focus = field, where,
stages and the runs it read), `enrich_case` rows with the source unit's stage, kind, key and
sample, one `enrich_prediction` per case (an origin run's boundary is derived when its proposals
are written), and proposals via backfill.propagate(). The runs chain: a newer run's proposal
supersedes an older run's for the same message and field (status 'superseded'), so the latest
run's answer is the one accept sees.

**Several fields at once** (`--field kind,topic,…`, run_many). The fields' cases overlap a lot, so
asking each field alone sends the same records again and again. A combined run takes each field's
hits (keep's are value's cases whose keep side is unsure), merges them per unit, and asks every
unit once with exactly the fields any of its cases hit: one request, each field's question as it
is asked alone, and the answers stored per field as a single run's are. A unit asked origin is
asked sender_kind directly too (COMPANION), so its boundary is not derived from the new origin
over a direct answer it already had. On one archive the eight improvement jobs
were 160,931 cases between them and 64,773 combined; 2,360 input tokens a case against 3,200 for
the same questions asked one field at a time.

**The answer key first.** `--gold` asks the same question of answer key 1 (a 'gold-eval' run with
params.focus) and reports its accuracy beside the full question set's. A run on the archive is
refused until such a run exists for exactly this question (its question version). Dry runs send
nothing, read no key and write nothing (a read-only transaction); they print the cases, the
estimated tokens per case (against the full set's on the same records) and the cost.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from collections import Counter
from typing import Callable

import psycopg
from psycopg.types.json import Jsonb

from talos import backfill, boundary, gold, jev, jev_gold, mask, personal, policy
from talos.backfill import BackfillError, Case
from talos.policy import Policy

PURPOSE = backfill.FOCUS_PURPOSE
FIELDS = ("sender_kind", "kind", "origin", "type", "topic", "value", "ask", "route")
WHERES = ("uncertain", "disagree", "all")
MAX_CASES = backfill.MAX_CASES
# The line under which a case is "uncertain": the two-level thresholds; ask and route as exact values.
THRESHOLDS = {**backfill.TWO_LEVEL, "ask": boundary.EXACT_MIN, "route": boundary.EXACT_MIN}
MARGIN = backfill.ACCEPT_MARGIN
NO_TEAMS = ("sender_kind", "origin")      # fixed on a Teams window: never asked
SENDER_INSTRUCTIONS = personal.wording("focus.sender_kind", (
    "Who produced {this}: a human, or a machine. People: a human wrote it to you or to a group or list you "
    "are in, also when a platform delivers the person's own words (a ticket reply, a LinkedIn or Blocket message, "
    "a document comment, an invitation someone sent). Machine: a system or a company produced it (receipts, "
    "notifications, alerts, reports, newsletters and marketing, automatic replies), even when signed by a person."))
KIND_INSTRUCTIONS = personal.wording("focus.kind", (
    "What {this} is to you, in one coarse word: how you read your mail. Pick the one kind that fits best, "
    "judged by what it is to you, not by the sender's wording. A person writing with you is a conversation unless "
    "they ask you something or invite you to a meeting. Notices about your own accounts, services and purchases "
    "that need no reply are fyi. Mass mailings are announcements, or offers when they sell."))


class FocusError(BackfillError):
    pass


# ---------------------------------------------------------------- the question

def dims_for(conn: psycopg.Connection, field: str) -> dict:
    """The dimensions a focused run reads answers for: the field alone."""
    return jev.taxonomy_from_db(conn, fields=(field,))


def questions(dims: dict, field: str, *, teams: bool = False) -> dict:
    """The one question for a field (ask: with its deadline statement), as a question set's entry."""
    if field not in FIELDS:
        raise FocusError(f"--field is one of {', '.join(FIELDS)}")
    d = dims[field]
    this = "this chat window" if teams else "this message"
    if field == "sender_kind":
        return {"sender_kind": {"type": "choice", "instructions": SENDER_INSTRUCTIONS.format(this=this),
                                "criteria": {v["value"]: jev._criterion(v) for v in d["values"]}}}
    if field == boundary.KIND:
        pre = jev.TEAMS_PREAMBLE if teams else ""
        return {"kind": {"type": "choice", "instructions": pre + KIND_INSTRUCTIONS.format(this=this),
                         "criteria": {v["value"]: jev._criterion(v) for v in d["values"]}}}
    qs = jev.build_questions({field: d}, teams=teams)
    return {k: v for k, v in qs.items() if k == field or k.startswith(field + ":")}


def question_sets(dims: dict, field: str) -> dict:
    return {m: questions(dims, field, teams=m == "teams") for m in jev.MEDIA}


def question_version(qsets: dict) -> str:
    return jev.question_version(qsets)


# ---------------------------------------------------------------- which cases

def _chain(conn: psycopg.Connection, stages: list[str], exclude: str | None = None) -> list[dict]:
    """The runs whose answers count, oldest first: the latest backfill run of each stage, and
    every focused run made after the first of them."""
    src = conn.execute("select distinct on (params->>'stage') id, created_at, params->>'stage' as stage from model_run"
                       " where purpose = %s and params->>'stage' = any(%s)"
                       " order by params->>'stage', created_at desc, id desc", (backfill.PURPOSE, stages)).fetchall()
    if not src:
        raise FocusError(f"no backfill run for {', '.join(stages)}: run talos enrich jev backfill first")
    first = min(r["created_at"] for r in src)
    # Focused runs since, and the incremental runs over new mail (talos.incremental), whose cases
    # are asked or reused units of the same kinds.
    focus = conn.execute("select id, created_at, null as stage from model_run where purpose = any(%s)"
                         " and created_at > %s and id <> coalesce(%s, '')",
                         ([PURPOSE, backfill.INCREMENTAL_PURPOSE], first, exclude)).fetchall()
    return sorted(src + focus, key=lambda r: (r["created_at"], r["id"]))


def _latest(conn: psycopg.Connection, field: str, chain: list[str], stages: list[str]) -> list[dict]:
    """Per case of the chain (stage, unit, sample): the newest answer for the field, with its
    confidence and margin; a boundary (or kind) asked directly, or derived from its source's scores."""
    p = {"runs": chain, "f": field, "stages": stages}
    if field in boundary.DERIVED:
        p.update(boundary.params(boundary.sides(conn)))
        preds = (f"select case_id, field, top, array[]::text[] as selected, confidence, margin, false as fixed"
                 f" from enrich_prediction where field = %(f)s"
                 f" union all select case_id, field, top, array[]::text[], confidence, margin, fixed"
                 f" from ({boundary.DERIVED_SQL}) d where d.field = %(f)s")
    else:
        preds = ("select case_id, field, top, selected, confidence, margin, fixed from enrich_prediction"
                 " where field = %(f)s")
    return conn.execute(
        f"select distinct on (c.stage, c.unit_key, c.sample_no) c.id as case_id, c.run_id, c.stage, c.unit_kind,"
        f" c.unit_key, c.sample_no, c.anchor_id, c.member_ids, p.top, p.selected, p.confidence, p.margin"
        f" from enrich_case c join ({preds}) p on p.case_id = c.id join model_run r on r.id = c.run_id"
        f" where c.run_id = any(%(runs)s) and c.error is null and not p.fixed and c.stage = any(%(stages)s)"
        f" order by c.stage, c.unit_key, c.sample_no, r.created_at desc, r.id desc", p).fetchall()


def _uncertain(row: dict, field: str) -> bool:
    t = THRESHOLDS[field]
    conf = row["confidence"] if row["confidence"] is not None else 0.0
    if field in boundary.DERIVED:
        return conf < t - 1e-9
    return conf < t - 1e-9 or (row["margin"] if row["margin"] is not None else 0.0) < MARGIN - 1e-9


def _rule_values(conn: psycopg.Connection, anchors: list[int], field: str) -> dict[int, set[str]]:
    """The anchors' effective values that a rule, the pre-pass or the owner set, per message. For
    sender_kind: its own, or else the origin's side."""
    out: dict[int, set[str]] = {}
    dims = [field] + ([boundary.SOURCE[field]] if field in boundary.DERIVED else [])
    rows = conn.execute("select e.message_id, e.dimension_id, e.value from unnest(%s::bigint[]) a(id)"
                        " join effective_message_assignment e on e.message_id = a.id"
                        " where e.dimension_id = any(%s) and e.source_kind in ('human', 'rule')",
                        (anchors, dims)).fetchall()
    side_of = boundary.sides(conn)[field]["side_of"] if field in boundary.DERIVED else {}
    for r in rows:
        if r["dimension_id"] == field:
            out.setdefault(r["message_id"], set()).add(r["value"])
    for r in rows:  # a boundary's source counts only where the boundary itself has no rule value
        if r["dimension_id"] != field and r["message_id"] not in out and side_of.get(r["value"]):
            out.setdefault(r["message_id"], set()).add(side_of[r["value"]])
    return out


def _disagrees(row: dict, field: str, rule: set[str] | None) -> bool:
    if not rule:
        return False
    if field in gold.MANY_FIELDS:
        return set(row["selected"] or ()) != rule
    return row["top"] not in rule


def _hits(conn: psycopg.Connection, field: str, where: str, latest: list[dict]) -> list[dict]:
    if where == "uncertain":
        return [r for r in latest if _uncertain(r, field)]
    if where == "disagree":
        rules = _rule_values(conn, sorted({r["anchor_id"] for r in latest}), field)
        return [r for r in latest if _disagrees(r, field, rules.get(r["anchor_id"]))]
    return latest


def _since(conn: psycopg.Connection, rows: list[dict], since: int | None) -> list[dict]:
    """Only the units with a message in the last `since` days."""
    if since is None:
        return rows
    if since < 1:
        raise FocusError("--since is a number of days, at least 1")
    recent = {r["id"] for r in conn.execute(
        "select distinct m.id from unnest(%s::bigint[]) a(id) join message m on m.id = a.id"
        " where m.received_at >= now() - make_interval(days => %s)",
        (sorted({m for r in rows for m in r["member_ids"]}), since))}
    keep = {(r["stage"], r["unit_key"]) for r in rows if recent.intersection(r["member_ids"])}
    return [r for r in rows if (r["stage"], r["unit_key"]) in keep]


def select(conn: psycopg.Connection, field: str, where: str, *, stages: list[str] | None = None,
           since: int | None = None, exclude_run: str | None = None) -> dict:
    """The cases a focused run would ask, and why. Plain SELECTs: works read-only."""
    if field not in FIELDS:
        raise FocusError(f"--field is one of {', '.join(FIELDS)}")
    if where not in WHERES:
        raise FocusError(f"--where is one of {', '.join(WHERES)}")
    allowed = [s for s in backfill.STAGES if not (field in NO_TEAMS and s == "teams")]
    stages = [s for s in (stages or allowed) if s in allowed]
    if not stages:
        raise FocusError(f"{field} is fixed on Teams windows: pick --stage machine or person")
    chain = _chain(conn, stages, exclude_run)
    latest = _latest(conn, field, [r["id"] for r in chain], stages)
    hit = _hits(conn, field, where, latest)
    units = {(r["stage"], r["unit_key"]) for r in hit}
    source = {(r["stage"], r["unit_key"], r["sample_no"]): r for r in latest}
    rows = _since(conn, [r for r in latest if (r["stage"], r["unit_key"]) in units], since)
    todo = _cases(conn, rows)
    return {"field": field, "where": where, "stages": stages, "since": since, "chain": [r["id"] for r in chain],
            "answered": len(latest), "hit": len(hit), "units": len({(c.stage, c.key) for c in todo}),
            "cases": todo, "source": source}


def _cases(conn: psycopg.Connection, rows: list[dict]) -> list[Case]:
    """The source cases as the backfill built them (talos.backfill: _machine, _person, _teams),
    from what enrich_case stored: the same unit, anchor, members and context, so the record is
    the one the backfill sent (the stored record hash says so)."""
    anchors: dict[tuple, list] = {}
    for r in rows:
        anchors.setdefault((r["stage"], r["unit_key"]), []).append((r["sample_no"], r["anchor_id"]))
    threads = sorted({int(backfill.base_key(r["unit_key"]).split(":", 1)[1]) for r in rows
                      if r["unit_kind"] == "thread" and r["unit_key"].startswith("thread:")})
    sizes = {t["id"]: t["message_count"] for t in conn.execute(
        "select id, message_count from thread where id = any(%s)", (threads,))} if threads else {}
    out = []
    for r in sorted(rows, key=lambda r: (backfill.STAGES.index(r["stage"]), r["unit_key"], r["sample_no"])):
        kind, key, members, anchor = r["unit_kind"], r["unit_key"], list(r["member_ids"]), r["anchor_id"]
        base = backfill.base_key(key)  # an incremental case's key has its anchor after "~"
        if kind == "template":
            ctx = [a for _, a in sorted(anchors[(r["stage"], key)]) if a != anchor]
            load = {"kind": "pattern", "context_ids": ctx, "pattern_key": base.removeprefix("template:")}
        elif kind == "tail":
            load = ({"kind": "pattern", "context_ids": [m for m in members if m != anchor],
                     "pattern_key": base.removeprefix("tail:")} if len(members) == 2 else {"kind": "message"})
        elif kind == "thread":
            tid = int(base.split(":", 1)[1]) if base.startswith("thread:") else None
            load = {"kind": "thread", "thread_id": tid} if tid and (sizes.get(tid) or 0) > 1 else {"kind": "message"}
        else:
            _, tid, _first = base.split(":")
            load = {"kind": "window", "thread_id": int(tid), "window_first_id": members[0],
                    "window_last_id": members[-1]}
        out.append(Case(r["stage"], kind, key, anchor, members, r["sample_no"], load, len(members)))
    return out


def _context(conn: psycopg.Connection, field: str, todo: list[Case], source: dict) -> dict:
    """For ask and route: the origin and topic scores each case's source had, so the gates route
    the focused answer as they routed the full one."""
    if field not in gold.MANY_FIELDS or not todo:
        return {}
    ids = {(c.stage, c.key, c.sample): source[(c.stage, c.key, c.sample)]["case_id"]
           for c in todo if (c.stage, c.key, c.sample) in source}
    by_case: dict[int, dict] = {}
    for r in conn.execute("select case_id, field, scores from enrich_prediction where case_id = any(%s)"
                          " and field in ('origin', 'topic')", (list(ids.values()),)):
        by_case.setdefault(r["case_id"], {})[r["field"]] = r["scores"]
    return {(k[1], k[2]): by_case.get(cid, {}) for k, cid in ids.items()}


# ---------------------------------------------------------------- estimate and the gate

def estimate(conn: psycopg.Connection, todo: list[Case], qsets: dict, model: str) -> dict:
    """Tokens per case for the focused question and, on the same records, for the full question
    set, from an even sample of the cases (calibrated as the backfill's estimate is)."""
    if not todo:
        return {"cases": 0, "sampled": 0, "input_tokens_per_case": 0, "full_tokens_per_case": 0, "input_tokens": 0,
                "cost_usd": 0.0, "full_cost_usd": 0.0, "seconds": 0.0, "example": None}
    step = max(1, len(todo) // backfill.ESTIMATE_SAMPLE)
    picked = todo[::step][:backfill.ESTIMATE_SAMPLE]
    built = [b for b in backfill._build(conn, picked, qsets, model) if b[3] is None]
    full = jev.question_sets(jev.taxonomy_from_db(conn))
    cal = lambda rec: backfill.CALIBRATION["teams" if rec.get("unit") == "window" else "email"]  # noqa: E731
    per = statistics.mean(jev.estimate_tokens(b[2]) * cal(b[1]) for b in built) if built else 0
    per_full = statistics.mean(jev.estimate_tokens(jev.request_body(b[1], jev.questions_for(full, b[1]), model=model))
                               * cal(b[1]) for b in built) if built else 0
    tokens = round(per * len(todo))
    return {"cases": len(todo), "sampled": len(built), "input_tokens_per_case": round(per),
            "full_tokens_per_case": round(per_full), "input_tokens": tokens, "cost_usd": round(jev.cost(tokens), 4),
            "full_cost_usd": round(jev.cost(round(per_full * len(todo))), 4),
            "seconds": round(len(todo) / backfill.MEASURED_CASES_PER_SECOND, 1),
            "example": built[0] if built else None}


def gold_runs(conn: psycopg.Connection, field: str, qversion: str) -> list[dict]:
    """The answer-key runs of exactly this focused question: asked alone, or in a combined run
    (params.focus.question_versions holds each field's own version)."""
    return conn.execute("select id, created_at, params from model_run where purpose = %s"
                        " and ((params->'focus'->>'field' = %s and params->>'question_version' = %s)"
                        "      or params->'focus'->'question_versions'->>%s = %s)"
                        " order by created_at desc", (jev_gold.PURPOSE, field, qversion, field, qversion)).fetchall()


# ---------------------------------------------------------------- the run on the archive

def _params(field: str, sel: dict, qversion: str, pol: Policy, client: jev.JevClient | None) -> dict:
    return {"focus": {"field": field, "where": sel["where"], "stages": sel["stages"], "since": sel["since"],
                      "chain": sel["chain"], "threshold": THRESHOLDS[field]},
            "stage": None, "unit": backfill.UNIT, "question_version": qversion, "template_version": jev.TEMPLATE_VERSION,
            "record_version": jev.RECORD_VERSION, "mask_version": mask.VERSION, "policy": pol.as_dict(),
            "endpoint": client.endpoint if client else jev.ENDPOINT,
            "concurrency": client.concurrency if client else jev.CONCURRENCY,
            "price_per_mtok_input": jev.PRICE_PER_MTOK_INPUT,
            "cases_done": 0, "failed": 0, "requests": 0, "retries": 0, "input_tokens": 0, "output_tokens": 0,
            "cost_usd": 0.0, "wall_seconds": 0.0, "invocations": 0}


def _plan(conn: psycopg.Connection, field: str, where: str, stages, since, run_id, client) -> tuple:
    dims = dims_for(conn, field)
    qsets = question_sets(dims, field)
    qversion = question_version(qsets)
    model = client.model if client else jev.MODEL
    existing = None
    if run_id:
        backfill.check_run_id(run_id)
        existing = conn.execute("select * from model_run where id = %s", (run_id,)).fetchone()
        if existing and (existing["purpose"] != PURPOSE or existing["params"].get("question_version") != qversion
                         or existing["params"]["focus"]["field"] != field):
            raise FocusError(f"run {run_id} is not a focused {field} run with this question; start a new run instead")
    t0 = time.monotonic()
    sel = select(conn, field, where, stages=stages, since=since, exclude_run=run_id)
    done = set()
    if existing:
        done = {(r["unit_key"], r["sample_no"]) for r in conn.execute(
            "select unit_key, sample_no from enrich_case where run_id = %s and error is null", (run_id,))}
    todo = [c for c in sel["cases"] if (c.key, c.sample) not in done]
    est = estimate(conn, todo, qsets, model)
    example = est.pop("example", None)
    gates = gold_runs(conn, field, qversion)
    kinds = Counter(c.kind for c in todo)
    summary = {"field": field, "where": where, "stages": sel["stages"], "since": since, "run_id": run_id,
               "model": model, "question_version": qversion, "chain": sel["chain"], "answered": sel["answered"],
               "hit": sel["hit"], "units": sel["units"], "already_done": len(done),
               "to_send": len(todo), "kinds": dict(kinds), "messages": len({m for c in todo for m in c.members}),
               "estimate": est, "select_seconds": round(time.monotonic() - t0, 2),
               "gold_run": gates[0]["id"] if gates else None,
               "request": example[2] if example else None,
               "example_unit": {"key": example[0].key, "anchor": example[0].anchor, "kind": example[0].kind}
               if example else None}
    return summary, sel, todo, existing, dims, qsets, qversion, model


def run(conn: psycopg.Connection, field: str, where: str, *, stages: list[str] | None = None,
        since: int | None = None, client: jev.JevClient | None = None, run_id: str | None = None,
        max_cases: int = MAX_CASES, budget: float | None = None, dry_run: bool = False,
        pol: Policy = policy.DEFAULT, chunk: int = backfill.CHUNK,
        progress: Callable[[str], None] = print) -> dict:
    """A focused run on the archive (or its dry run). See the module docstring."""
    if dry_run:
        idle = conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        with conn.transaction():
            if idle:
                conn.execute("set transaction read only")
            summary, *_ = _plan(conn, field, where, stages, since, run_id, client)
        summary["over_max_cases"] = summary["to_send"] > max_cases
        summary["over_budget"] = budget is not None and summary["estimate"]["cost_usd"] > budget
        return summary
    summary, sel, todo, existing, dims, qsets, qversion, model = _plan(conn, field, where, stages, since, run_id, client)
    if summary["gold_run"] is None:
        raise FocusError(f"refused: run this question on the answer key first: talos enrich jev focus --field {field}"
                         f" --gold (question version {qversion})")
    cost = summary["estimate"]["cost_usd"]
    if len(todo) > max_cases:
        raise FocusError(f"refused: {len(todo):,} cases to send is over --max-cases {max_cases:,} (${cost:.2f});"
                         " raise it, or use --since or --stage")
    if budget is not None and cost > budget:
        raise FocusError(f"refused: the estimate ${cost:.2f} for {len(todo):,} cases is over --budget ${budget:.2f}")
    summary.pop("request", None)
    if client is None:
        client = jev.JevClient()
    if not existing:
        run_id = run_id or f"jev-focus-{field}-{where}-{qversion[:8]}-{jev.now_stamp()}"
        backfill.check_run_id(run_id)
        conn.execute("insert into model_run (id, model, purpose, params, input_count) values (%s, %s, %s, %s, %s)",
                     (run_id, model, PURPOSE, Jsonb(_params(field, sel, qversion, pol, client)), len(sel["cases"])))
    if not conn.autocommit:
        conn.commit()
    summary["run_id"] = run_id
    progress(f"focus {field} ({where}): run {run_id}: {len(todo):,} cases, about"
             f" {summary['estimate']['input_tokens']:,} input tokens (${cost:.2f})")
    state, before = backfill._send(conn, run_id, f"focus {field}", todo, client, qsets, dims, pol, model,
                                   chunk=chunk, budget=budget, progress=progress,
                                   context=_context(conn, field, todo, sel["source"]))
    summary.update(stored=state["stored"], failed=[{"unit": k, "error": e} for k, e in state["failed"]],
                   usage={k: client.usage.as_dict()[k] - before[k] for k in before},
                   seconds=round(time.monotonic() - state["t0"], 2))
    summary["cost_usd"] = round(jev.cost(summary["usage"]["input_tokens"]), 6)
    if state["stopped"]:
        summary["error"] = state["stopped"]
    summary["propagation"] = backfill.propagate(conn, run_id)
    return summary


def format_dry_run(s: dict) -> str:
    e = s["estimate"]
    lines = ["DRY RUN: nothing is sent, no key is read, nothing is stored.",
             f"Focused question: {s['field']} alone, where {s['where']}, stages {', '.join(s['stages'])}"
             + (f", units with a message in the last {s['since']} days" if s["since"] else "")
             + f"; question version {s['question_version']}, model {s['model']}",
             f"read from {len(s['chain'])} run(s): {', '.join(s['chain'])}",
             f"{s['answered']:,} cases answered; {s['hit']:,} are {s['where']}"
             f" → {s['units']:,} units, {s['to_send']:,} cases to send"
             + (f" ({s['already_done']:,} already done)" if s["already_done"] else "")
             + f", standing for {s['messages']:,} messages (selected in {s['select_seconds']} s)",
             "  " + ", ".join(f"{k} {n:,}" for k, n in sorted(s["kinds"].items())),
             f"estimated input tokens per case: {e['input_tokens_per_case']:,} for this one question, against"
             f" {e['full_tokens_per_case']:,} for the full question set on the same records"
             f" (a sample of {e['sampled']})",
             f"estimated cost: {e['input_tokens']:,} input tokens, ${e['cost_usd']:.2f}"
             f" (the full set would be ${e['full_cost_usd']:.2f}); about {e['seconds'] / 60:.1f} min",
             ("answer key: checked by run " + s["gold_run"]) if s["gold_run"] else
             f"answer key: NOT yet asked; a real run is refused until: talos enrich jev focus --field {s['field']} --gold"]
    if s.get("over_max_cases"):
        lines.append("a real run would be REFUSED: over --max-cases")
    if s.get("over_budget"):
        lines.append("a real run would be REFUSED: the estimate is over --budget")
    if s.get("request") is not None:
        u = s["example_unit"]
        lines += ["", f"The request for {u['kind']} {u['key']} (anchor message {u['anchor']}), exactly as it would be sent:",
                  f"POST {jev.ENDPOINT}",
                  f"Authorization: Bearer <the Keychain item {jev.KEY_NAME!r}; not read in a dry run>",
                  "Content-Type: application/json", "",
                  json.dumps(s["request"], ensure_ascii=False, indent=2)]
    return "\n".join(lines)


# ---------------------------------------------------------------- several fields in one run

# keep is value's side, never asked alone: its unsure cases are asked value.
ASKED = {"keep": "value"}
# A unit asked origin is asked sender_kind directly too: otherwise its sender_kind would be derived
# from the new origin answer and replace a direct answer the unit already had.
COMPANION = {"origin": "sender_kind"}


def asked(field: str) -> str:
    return ASKED.get(field, field)


def parse_fields(spec: str | list[str]) -> list[str]:
    """--field kind,topic,keep: the fields, in the order given, once each."""
    fs = [f.strip() for f in (spec.split(",") if isinstance(spec, str) else spec) if f and f.strip()]
    bad = [f for f in fs if f not in FIELDS and f not in ASKED]
    if not fs or bad:
        raise FocusError(f"--field is one or more of {', '.join(FIELDS + tuple(ASKED))}, separated by commas")
    return list(dict.fromkeys(fs))


def select_many(conn: psycopg.Connection, fields: list[str], where: str, *, stages: list[str] | None = None,
                since: int | None = None, exclude_run: str | None = None) -> dict:
    """The cases a combined run asks: each field's hits, found as select() finds them (keep's are
    the cases whose keep side is unsure), merged per unit. A unit is asked every field any of its
    cases hits and only those, so a message unsure of three fields is sent once, not three times.
    Plain SELECTs: works read-only."""
    if where not in WHERES:
        raise FocusError(f"--where is one of {', '.join(WHERES)}")
    fields = parse_fields(fields)
    stages = [s for s in (stages or backfill.STAGES) if s in backfill.STAGES]
    chain = _chain(conn, stages, exclude_run)
    ids = [r["id"] for r in chain]
    unit_fields: dict[tuple, set[str]] = {}
    rows: dict[tuple, dict] = {}
    sources: dict[str, dict] = {}
    per_field = {}
    for f in fields:
        a = asked(f)
        allowed = [s for s in stages if not (a in NO_TEAMS and s == "teams")]
        latest = _latest(conn, f, ids, allowed) if allowed else []
        hit = _hits(conn, f, where, latest)
        units = {(r["stage"], r["unit_key"]) for r in hit}
        for r in latest:
            k = (r["stage"], r["unit_key"])
            if k in units:
                unit_fields.setdefault(k, set()).update({a} | ({COMPANION[a]} if a in COMPANION else set()))
                rows.setdefault((*k, r["sample_no"]), r)
        if a == f:
            sources[f] = {(r["stage"], r["unit_key"], r["sample_no"]): r for r in latest}
        per_field[f] = {"asked": a, "answered": len(latest), "hit": len(hit), "units": len(units),
                        "cases": sum(1 for r in latest if (r["stage"], r["unit_key"]) in units)}
    todo = _cases(conn, _since(conn, list(rows.values()), since))
    order = [asked(f) for f in fields] + [c for c in COMPANION.values()]
    unit_fields = {k: tuple(f for f in dict.fromkeys(order) if f in v) for k, v in unit_fields.items()}
    return {"fields": fields, "where": where, "stages": stages, "since": since, "chain": ids,
            "per_field": per_field, "unit_fields": unit_fields, "sources": sources,
            "units": len({(c.stage, c.key) for c in todo}), "cases": todo}


def _asking(conn: psycopg.Connection, sel: dict) -> tuple[dict, dict, dict, Callable]:
    """Each asked field's question sets and dimension, and per_case: a case's own merged question
    sets and dimensions (cached per combination of fields)."""
    asked_ = list(dict.fromkeys(f for fs in sel["unit_fields"].values() for f in fs))
    qsets = {f: question_sets(dims_for(conn, f), f) for f in asked_}
    dims = {f: dims_for(conn, f)[f] for f in asked_}
    cache: dict[tuple, tuple[dict, dict]] = {}

    def per_case(c: Case) -> tuple[dict, dict]:
        fs = sel["unit_fields"][(c.stage, c.key)]
        if fs not in cache:
            cache[fs] = ({m: {k: v for f in fs for k, v in qsets[f][m].items()} for m in jev.MEDIA},
                         {f: dims[f] for f in fs})
        return cache[fs]
    return qsets, dims, {f: question_version(qsets[f]) for f in asked_}, per_case


def estimate_many(conn: psycopg.Connection, todo: list[Case], qsets: dict, per_case: Callable, model: str) -> dict:
    """Tokens per case for the combined questions and, on the same records, for the same questions
    asked one field at a time (what the separate jobs would send), from an even sample."""
    if not todo:
        return {"cases": 0, "sampled": 0, "input_tokens_per_case": 0, "separate_tokens_per_case": 0,
                "input_tokens": 0, "cost_usd": 0.0, "separate_cost_usd": 0.0, "asks": 0, "seconds": 0.0}
    step = max(1, len(todo) // backfill.ESTIMATE_SAMPLE)
    picked = todo[::step][:backfill.ESTIMATE_SAMPLE]
    built = [b for b in backfill._build(conn, picked, {}, model, per_case) if b[3] is None]
    cal = lambda rec: backfill.CALIBRATION["teams" if rec.get("unit") == "window" else "email"]  # noqa: E731
    per = statistics.mean(jev.estimate_tokens(b[2]) * cal(b[1]) for b in built) if built else 0
    sep = statistics.mean(sum(jev.estimate_tokens(jev.request_body(b[1], jev.questions_for(qsets[f], b[1]),
                                                                   model=model)) * cal(b[1])
                              for f in per_case(b[0])[1]) for b in built) if built else 0
    tokens = round(per * len(todo))
    asks = sum(len(per_case(c)[1]) for c in todo)
    return {"cases": len(todo), "sampled": len(built), "input_tokens_per_case": round(per),
            "separate_tokens_per_case": round(sep), "input_tokens": tokens, "cost_usd": round(jev.cost(tokens), 4),
            "separate_cost_usd": round(jev.cost(round(sep * len(todo))), 4), "asks": asks,
            "seconds": round(len(todo) / backfill.MEASURED_CASES_PER_SECOND, 1),
            "example": built[0] if built else None}


def _plan_many(conn: psycopg.Connection, fields, where, stages, since, run_id, client) -> tuple:
    model = client.model if client else jev.MODEL
    existing = None
    if run_id:
        backfill.check_run_id(run_id)
        existing = conn.execute("select * from model_run where id = %s", (run_id,)).fetchone()
    t0 = time.monotonic()
    sel = select_many(conn, fields, where, stages=stages, since=since, exclude_run=run_id)
    qsets, dims, qversions, per_case = _asking(conn, sel)
    qversion = jev.question_version(qsets)
    if existing and (existing["purpose"] != PURPOSE or existing["params"]["focus"].get("fields") != sel["fields"]
                     or existing["params"]["focus"].get("where") != where):
        raise FocusError(f"run {run_id} is not a combined focused run of these fields; start a new run instead")
    done = set()
    if existing:
        done = {(r["unit_key"], r["sample_no"]) for r in conn.execute(
            "select unit_key, sample_no from enrich_case where run_id = %s and error is null", (run_id,))}
    todo = [c for c in sel["cases"] if (c.key, c.sample) not in done]
    est = estimate_many(conn, todo, qsets, per_case, model)
    example = est.pop("example", None)
    unchecked = [f for f, qv in qversions.items() if not gold_runs(conn, f, qv)]
    combos = Counter(sel["unit_fields"][(c.stage, c.key)] for c in todo)
    summary = {"fields": sel["fields"], "where": where, "stages": sel["stages"], "since": since, "run_id": run_id,
               "model": model, "question_version": qversion, "question_versions": qversions, "chain": sel["chain"],
               "per_field": sel["per_field"], "units": sel["units"], "already_done": len(done), "to_send": len(todo),
               "kinds": dict(Counter(c.kind for c in todo)), "messages": len({m for c in todo for m in c.members}),
               "combinations": [{"fields": list(k), "cases": n} for k, n in combos.most_common(8)],
               "estimate": est, "select_seconds": round(time.monotonic() - t0, 2), "unchecked": unchecked,
               "request": example[2] if example else None}
    return summary, sel, todo, existing, dims, qsets, qversions, per_case, model


def run_many(conn: psycopg.Connection, fields: str | list[str], where: str, *, stages: list[str] | None = None,
             since: int | None = None, client: jev.JevClient | None = None, run_id: str | None = None,
             max_cases: int = MAX_CASES, budget: float | None = None, dry_run: bool = False,
             pol: Policy = policy.DEFAULT, chunk: int = backfill.CHUNK,
             progress: Callable[[str], None] = print) -> dict:
    """Several focused jobs as one run on the archive: select_many's cases, each asked its own
    fields in one request. Stored as a focused run (params.focus.fields); each case's answers are
    stored per field as a single run's are, so the runs chain, propagation and accept treat it as
    the separate runs it replaces. Every field's question must have been checked on the answer
    key (alone, or in a combined gold run: gold_run with a list)."""
    fields = parse_fields(fields)
    if dry_run:
        idle = conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        with conn.transaction():
            if idle:
                conn.execute("set transaction read only")
            summary, *_ = _plan_many(conn, fields, where, stages, since, run_id, client)
        summary["over_max_cases"] = summary["to_send"] > max_cases
        summary["over_budget"] = budget is not None and summary["estimate"]["cost_usd"] > budget
        return summary
    summary, sel, todo, existing, dims, qsets, qversions, per_case, model = _plan_many(
        conn, fields, where, stages, since, run_id, client)
    if summary["unchecked"]:
        raise FocusError(f"refused: ask the answer key first, for {', '.join(summary['unchecked'])}:"
                         f" talos enrich jev focus --field {','.join(fields)} --gold")
    cost = summary["estimate"]["cost_usd"]
    if len(todo) > max_cases:
        raise FocusError(f"refused: {len(todo):,} cases to send is over --max-cases {max_cases:,} (${cost:.2f});"
                         " raise it, or use --since or --stage")
    if budget is not None and cost > budget:
        raise FocusError(f"refused: the estimate ${cost:.2f} for {len(todo):,} cases is over --budget ${budget:.2f}")
    summary.pop("request", None)
    if client is None:
        client = jev.JevClient()
    if not existing:
        run_id = run_id or f"jev-focus-combined-{where}-{summary['question_version'][:8]}-{jev.now_stamp()}"
        backfill.check_run_id(run_id)
        params = _params(fields[0], {**sel, "where": where}, summary["question_version"], pol, client)
        params["focus"] = {"field": None, "fields": sel["fields"], "question_versions": qversions, "where": where,
                           "stages": sel["stages"], "since": since, "chain": sel["chain"],
                           "thresholds": {f: THRESHOLDS[f] for f in sel["fields"]}}
        conn.execute("insert into model_run (id, model, purpose, params, input_count) values (%s, %s, %s, %s, %s)",
                     (run_id, model, PURPOSE, Jsonb(params), len(sel["cases"])))
    if not conn.autocommit:
        conn.commit()
    summary["run_id"] = run_id
    progress(f"focus {','.join(fields)} ({where}): run {run_id}: {len(todo):,} cases, about"
             f" {summary['estimate']['input_tokens']:,} input tokens (${cost:.2f})")
    context: dict = {}
    for f in gold.MANY_FIELDS:  # ask and route route with the origin and topic their source case had
        if f in sel["sources"]:
            context.update(_context(conn, f, [c for c in todo if f in per_case(c)[1]], sel["sources"][f]))
    state, before = backfill._send(conn, run_id, "focus combined", todo, client, {}, {}, pol, model,
                                   chunk=chunk, budget=budget, progress=progress, context=context, per_case=per_case)
    summary.update(stored=state["stored"], failed=[{"unit": k, "error": e} for k, e in state["failed"]],
                   usage={k: client.usage.as_dict()[k] - before[k] for k in before},
                   seconds=round(time.monotonic() - state["t0"], 2))
    summary["cost_usd"] = round(jev.cost(summary["usage"]["input_tokens"]), 6)
    if state["stopped"]:
        summary["error"] = state["stopped"]
    summary["propagation"] = backfill.propagate(conn, run_id)
    return summary


def format_dry_run_many(s: dict) -> str:
    e = s["estimate"]
    lines = ["DRY RUN: nothing is sent, no key is read, nothing is stored.",
             f"Combined focused run: {', '.join(s['fields'])}, where {s['where']}, stages {', '.join(s['stages'])}"
             + (f", units with a message in the last {s['since']} days" if s["since"] else "")
             + f"; question version {s['question_version']}, model {s['model']}",
             f"read from {len(s['chain'])} run(s)"]
    for f, p in s["per_field"].items():
        lines.append(f"  {f:<12} {p['hit']:>7,} {s['where']} of {p['answered']:,} → {p['units']:,} units,"
                     f" {p['cases']:,} cases" + (f" (asked as {p['asked']})" if p["asked"] != f else ""))
    sep_cases = sum(p["cases"] for p in s["per_field"].values())
    lines += [f"separately {sep_cases:,} cases; combined {s['to_send']:,} cases in {s['units']:,} units"
              + (f" ({s['already_done']:,} already done)" if s["already_done"] else "")
              + f", {e['asks']:,} field questions, standing for {s['messages']:,} messages"
              f" (selected in {s['select_seconds']} s)",
              "  most common combinations: " + "; ".join(f"{'+'.join(c['fields'])} {c['cases']:,}"
                                                          for c in s["combinations"]),
              f"estimated input tokens per case: {e['input_tokens_per_case']:,} combined, against"
              f" {e['separate_tokens_per_case']:,} for the same questions one field at a time",
              f"estimated cost ${e['cost_usd']:.2f} (separately ${e['separate_cost_usd']:.2f}),"
              f" about {e['seconds'] / 60:.0f} min"]
    if s["unchecked"]:
        lines.append(f"refused for now: ask the answer key first for {', '.join(s['unchecked'])}:"
                     f" talos enrich jev focus --field {','.join(s['fields'])} --gold")
    if s.get("over_max_cases"):
        lines.append(f"over --max-cases: pass --max-cases {s['to_send']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- the answer key first

def reference_run(conn: psycopg.Connection, set_id: int) -> dict | None:
    """The full question set's run to compare with: the latest v2 context run on the set with the
    backfill's question version (else the latest v2 run)."""
    qv = conn.execute("select params->>'question_version' as qv from model_run where purpose = %s"
                      " order by created_at desc limit 1", (backfill.PURPOSE,)).fetchone()
    rows = conn.execute("select * from model_run where purpose = %s and (params->>'set_id')::int = %s"
                        " and params->'focus' is null and coalesce((params->>'template_version')::int, 1) >= 2"
                        " order by created_at desc", (jev_gold.PURPOSE, set_id)).fetchall()
    same = [r for r in rows if qv and r["params"].get("question_version") == qv["qv"]]
    return (same or rows or [None])[0]


def _gold_scores(conn: psycopg.Connection, run_id: str, field: str, sd: dict | None) -> dict[int, dict]:
    """{item: {top, confidence, margin}} for a field from an answer-key run: stored, or for a
    boundary derived from the source field's full scores (a Teams window: people, fixed)."""
    out = {}
    src = boundary.SOURCE.get(field)
    for r in conn.execute("select item_id, field, top, scores, confidence, margin from jev_prediction"
                          " where run_id = %s and field = any(%s)", (run_id, [field] + ([src] if src else []))):
        if r["field"] == field:
            out[r["item_id"]] = {"top": r["top"], "confidence": r["confidence"], "margin": r["margin"]}
        elif r["item_id"] not in out and sd:
            probs = boundary.side_probabilities(r["scores"], sd[field]["side_of"], sd[field]["sides"])
            top, p, m, _ = boundary.decide(probs)
            out[r["item_id"]] = {"top": top, "confidence": p, "margin": m}
    return out


def gold_accuracy(conn: psycopg.Connection, set_id: int, field: str, runs: list[str]) -> dict:
    """Per run: over the items labelled for sure (a boundary or kind: the reference's own answer for
    it where the set labels it, else the side of its source field), how many it decides at the
    two-level threshold, how many of those are right, and the top answer's accuracy over all. Only
    the items every run answered count."""
    sd = boundary.sides(conn) if field in boundary.DERIVED else None
    ref, _ = gold.combined_labels(conn, set_id)
    src = boundary.SOURCE.get(field, field)
    truth = {}
    for i, labels in ref.items():
        own = labels.get(field) if field != src else None
        g = own if own is not None else labels.get(src)
        if not g or g["status"] != "set":
            continue
        if field in gold.MANY_FIELDS:
            truth[i] = set(g["values"])
        elif sd and own is None:
            truth[i] = sd[field]["side_of"].get(g["values"][0]) if g["values"] else None
        else:
            truth[i] = g["values"][0] if g["values"] else None
    scores = {r: _gold_scores(conn, r, field, sd) for r in runs}
    items = set(truth).intersection(*(set(v) for v in scores.values())) if scores else set()
    out = {"field": field, "items": len(items), "threshold": THRESHOLDS[field], "runs": {}}
    for r, sc in scores.items():
        decided = right = top_right = 0
        mistakes: Counter = Counter()
        for i in items:
            s = sc[i]
            want = truth[i]
            if field in gold.MANY_FIELDS:
                got = set() if s["top"] in (None, policy.NONE) else {s["top"]}
                ok = got == want
            else:
                ok = s["top"] == want
            top_right += ok
            if not _uncertain(s, field):
                decided += 1
                right += ok
                if not ok:
                    mistakes[f"{s['top']}→{','.join(sorted(want)) if isinstance(want, set) else want}"] += 1
        out["runs"][r] = {"decided": decided, "right": right, "top1_right": top_right,
                          "coverage": gold._rate(decided, len(items)), "accuracy": gold._rate(right, decided),
                          "top1": gold._rate(top_right, len(items)), "mistakes": dict(mistakes.most_common(6))}
    return out


def gold_run(conn: psycopg.Connection, set_id: int, field: str | list[str], *, client: jev.JevClient | None = None,
             dry_run: bool = False, pol: Policy = policy.DEFAULT, progress: Callable[[str], None] = print) -> dict:
    """The focused question on an answer key: every item but a Teams window where the field is
    fixed there. Stored as a 'gold-eval' run with params.focus; returns its accuracy beside the
    full question set's (reference_run) on the same items. dry_run: count, estimate, show one.

    Several fields (a list) are asked together, as a combined run on the archive asks them
    (run_many): one request per item, each field's question as it is asked alone. The run's
    params.focus.question_versions holds each field's own version, so it clears the gate for
    each of them; its accuracy is reported per field, beside the full set's and the field's
    latest run alone."""
    many = not isinstance(field, str)
    fields = [asked(f) for f in parse_fields(field)] if many else [field]
    fields = list(dict.fromkeys(fields))
    per = {f: question_sets(dims_for(conn, f), f) for f in fields}
    dims = {f: dims_for(conn, f)[f] for f in fields}
    qversions = {f: question_version(per[f]) for f in fields}
    qversion = qversions[fields[0]] if not many else jev.question_version(per)
    model = client.model if client else jev.MODEL

    def ask_of(rec: dict) -> list[str]:
        return [f for f in fields if not (f in NO_TEAMS and rec.get("unit") == "window")]

    def body(rec: dict, fs: list[str]) -> dict:
        m = "teams" if rec.get("unit") == "window" else "email"
        return jev.request_body(rec, {k: v for f in fs for k, v in per[f][m].items()}, model=model)

    cases = [c for c in jev_gold.plan(conn, set_id, backfill.UNIT) if ask_of(c["record"])]
    bodies = [(c["item_id"], body(c["record"], ask_of(c["record"]))) for c in cases]
    full = jev.question_sets(jev.taxonomy_from_db(conn))
    per_tok = statistics.mean(jev.estimate_tokens(b) for _, b in bodies) if bodies else 0
    per_full = statistics.mean(jev.estimate_tokens(jev.request_body(c["record"], jev.questions_for(full, c["record"]),
                                                                    model=model)) for c in cases) if cases else 0
    ref = reference_run(conn, set_id)
    summary = {"set_id": set_id, "field": ",".join(fields) if many else field, "fields": fields,
               "question_version": qversion, "question_versions": qversions, "model": model, "to_send": len(bodies),
               "input_tokens_per_case": round(per_tok), "full_tokens_per_case": round(per_full),
               "cost_usd": round(jev.cost(per_tok * len(bodies)), 4), "reference_run": ref["id"] if ref else None}
    if dry_run:
        summary["dry_run"] = True
        summary["request"] = bodies[0][1] if bodies else None
        return summary
    if len(bodies) > jev_gold.MAX_CASES:
        raise FocusError(f"refused: {len(bodies)} cases is over {jev_gold.MAX_CASES}")
    alone = {f: [r["id"] for r in gold_runs(conn, f, qversions[f]) if r["params"]["focus"].get("field") == f][:1]
             for f in fields} if many else {}
    client = client or jev.JevClient()
    run_id = f"jev-gold{set_id}-focus-{'combined' if many else field}-{qversion[:8]}-{jev.now_stamp()}"
    focus_params = {"fields": fields, "question_versions": qversions} if many else {"field": field}
    params = {**jev_gold._params(set_id, backfill.UNIT, qversion, pol, client, None), "focus": focus_params}
    conn.execute("insert into model_run (id, model, purpose, params, input_count) values (%s, %s, %s, %s, %s)",
                 (run_id, model, jev_gold.PURPOSE, Jsonb(params), len(bodies)))
    ctx = {}
    if set(gold.MANY_FIELDS) & set(fields) and ref:  # the gates route with the full run's origin and topic
        for r in conn.execute("select item_id, field, scores from jev_prediction where run_id = %s"
                              " and field in ('origin', 'topic')", (ref["id"],)):
            ctx.setdefault(r["item_id"], {})[r["field"]] = r["scores"]
    by_id = {c["item_id"]: c for c in cases}
    failed = []

    def on_result(res: jev.Result) -> None:
        c = by_id[res.case_id]
        if res.error is None:
            try:
                rec = c["record"]
                fields_ = jev.read_answers({f: dims[f] for f in ask_of(rec)}, res.response.get("answers"),
                                           teams=rec.get("unit") == "window")
                answers = {f: a["scores"] for f, a in fields_.items()}
                decisions, fired = policy.route_case({**ctx.get(c["item_id"], {}), **answers}, pol,
                                                     template=jev.TEMPLATE_VERSION)
                with conn.transaction():
                    conn.execute("insert into jev_case (run_id, item_id, record_sha256, record_chars, model,"
                                 " input_tokens, output_tokens, elapsed_ms) values (%s, %s, %s, %s, %s, %s, %s, %s)",
                                 (run_id, c["item_id"], jev.record_sha(rec), jev.record_chars(rec),
                                  res.response.get("model") or model, res.usage.get("input_tokens", 0),
                                  res.usage.get("output_tokens", 0), int(res.seconds * 1000)))
                    for f, a in fields_.items():
                        d = decisions[f]
                        raw = {**a["raw"], "gate": fired[f]} if f in fired else a["raw"]
                        conn.execute("insert into jev_prediction (run_id, item_id, field, top, selected, scores,"
                                     " confidence, margin, decided, raw) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                                     (run_id, c["item_id"], f, d.top, list(d.selected), Jsonb(a["scores"]),
                                      d.confidence, d.margin, d.decided, Jsonb(raw)))
            except jev.JevError as exc:
                res.error = str(exc)
        if res.error is not None:
            failed.append((c["position"], res.error))

    before = client.usage.as_dict()
    t0 = time.monotonic()
    try:
        asyncio.run(client.run(bodies, on_result))
    except jev.JevAuthError as exc:
        summary["error"] = str(exc)
    finally:
        jev_gold._account(conn, run_id, client, before, time.monotonic() - t0, len(failed))
    summary.update(run_id=run_id, failed=[{"position": p, "error": e} for p, e in failed],
                   usage={k: client.usage.as_dict()[k] - before[k] for k in before})
    if many:
        summary["accuracies"] = {f: gold_accuracy(conn, set_id, f, [run_id] + alone[f] + ([ref["id"]] if ref else []))
                                 for f in fields}
    else:
        summary["accuracy"] = gold_accuracy(conn, set_id, field, [run_id] + ([ref["id"]] if ref else []))
    progress(f"focus {summary['field']} on answer key {set_id}: run {run_id}, {len(bodies) - len(failed)} answered")
    return summary


def format_gold(s: dict) -> str:
    lines = [("DRY RUN: nothing is sent, no key is read, nothing is stored. " if s.get("dry_run") else "")
             + f"Focused question {s['field']} on answer key {s['set_id']} (question version {s['question_version']}):"
             f" {s['to_send']} items, about {s['input_tokens_per_case']:,} input tokens per case against"
             f" {s['full_tokens_per_case']:,} for the full set, ${s['cost_usd']:.4f}"]
    accs = s.get("accuracies") or ({s["field"]: s["accuracy"]} if s.get("accuracy") else {})
    for f, acc in accs.items():
        lines.append((f"{f}: " if s.get("accuracies") else "")
                     + f"Over {acc['items']} items labelled for sure; decided at ≥ {acc['threshold']:.2f}"
                     + ("" if f in boundary.DERIVED else f" with a margin ≥ {MARGIN:.2f}") + ":")
        for r, d in acc["runs"].items():
            who = ("combined" if s.get("accuracies") else "focused") if r == s.get("run_id") \
                else "full set" if r == s.get("reference_run") else "alone"
            lines.append(f"  {who:<8} {r}: decided {d['decided']}/{acc['items']} ({gold.pct(d['coverage'])}),"
                         f" right {d['right']}/{d['decided']} ({gold.pct(d['accuracy'])}); top-1 {gold.pct(d['top1'])}"
                         + (("; mistakes " + ", ".join(f"{k} {n}" for k, n in d["mistakes"].items()))
                            if d["mistakes"] else ""))
    for f in s.get("failed") or []:
        lines.append(f"  item {f['position']}: {f['error']}")
    if s.get("dry_run") and s.get("request") is not None:
        lines += ["", "One request, exactly as it would be sent:", json.dumps(s["request"], ensure_ascii=False, indent=2)]
    return "\n".join(lines)

