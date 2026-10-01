"""The enrichment backfill: Jev over the whole archive in collapsed units, stored as proposals.

docs/enrichment-plan.md §4.2 (collapsing), §5 (Teams windows), §6 (stages C–E) and §11
(decision 7: one pass first). Models propose, people decide: everything this writes is an
`assignment` with source_kind 'model' and status 'proposed', which the effective views ignore
until `accept()` makes it active under per-field thresholds. Nothing here touches a mailbox.

**Stages and units.** Machine and person mail are told apart as the answer key tells them
(gold._MACHINE / gold._PERSON: the effective origin, or with none, the machine signals):

| Stage     | Unit                                                 | Case(s)                                | Stands for                       |
|-----------|------------------------------------------------------|----------------------------------------|----------------------------------|
| `machine` | template (sender + subject skeleton) of 3+ messages  | 3 samples: first, middle, last in time | every machine message of it      |
|           | tail: a template of 1 or 2 messages                  | 1: its latest message                  | its message(s)                   |
| `person`  | thread (person-side incoming mail)                   | 1: anchored on its latest incoming one | its person-side incoming mail    |
| `teams`   | 2-hour window, split every TEAMS_WINDOW_CAP messages | 1: anchored on its latest incoming one | every message of the window      |

The tail is one case per *sender and template*, not one per sender: on the real archive that is
28,457 cases instead of 5,197 (about $6 and 15 minutes more), and a sender's small templates are
often different kinds of mail (an invoice, a delivery notice, a newsletter), which one sample
could not stand for. The windows are the answer key's (gold._pools): no gap over
enrich.TEAMS_WINDOW_GAP within a chat, split every TEAMS_WINDOW_CAP messages.

Every case is built as the answer key builds an item (gold.unit(), then jev.record() in the
`context` design): a template sample shows the template's other two samples; a thread its
other messages; a window its lines and the three before it. So a case asks exactly what the
v2 evaluation asked, with the same question sets (jev.question_sets).

**A run** is one stage: a `model_run` row (purpose 'enrich-backfill') with the stage, the
question, record and masking versions, the policy and running totals. Each case is an
`enrich_case` row (unit key, anchor, members, record hash and size, tokens, time, error) with its
`enrich_prediction` rows (top, scores, top 3, confidence, margin, decided, gates). Cases are sent
in chunks (CHUNK); a chunk's answers are written in one transaction, so a run stopped halfway
resumes where it was (`run_id=` the same): a case already stored is skipped, a failed one is
tried again. A resumed run must ask the same questions of the same kind of records.

**Guards.** A run refuses to send more than max_cases (60,000) cases in one go, or when its
estimate is over the budget; while it runs it stops once it has spent the budget. A dry run
reads no key, sends nothing and writes nothing (it works in a read-only transaction): it counts
the cases, builds a sample of their records to estimate tokens, cost and time, and shows one
request exactly as it would be sent.

**Proposals** (`propagate()`, run after every invocation): one row per message, field and value,
source_ref = the run id, confidence = the top probability, evidence = {case, unit, propagated,
scores (top 3), margin, decided, gate, question_version}.

- One-value fields (origin, type, topic, value): the top value, decided or not (`decided` in the
  evidence), so accept can apply its own thresholds. A Teams window's origin is fixed, not
  asked, and is never proposed.
- ask and route: one row per selected value (after the policy's gates); none writes nothing.
- A template's samples always get their own answers. The template's other messages get a field
  only when all three samples were answered and agree on it (the same top value, or for ask and
  route the same selection); their confidence and margin are the lowest of the three.
- A tail, thread or window case gives its answers to all of its members.
- A case Jev was not asked, that reuses earlier answers (talos.incremental; enrich_case.source),
  marks every row it gives as propagated, with evidence.propagated_from = its source.

A rerun of a run replaces that run's proposed rows; rows already accepted or rejected are kept
(and not proposed again). Nothing else is touched: human, rule, pre-pass and other runs' rows
keep theirs (every write filters source_kind 'model' and source_ref = the run).

**Boundaries** (talos.boundary, two-level acceptance): the proposals also carry sender_kind, sphere,
form and keep, each side's probability the sum of the full stored scores of its source field
(evidence: scores = both sides, boundary_of = the source; a Teams window's sender_kind is people,
fixed). **Kind** is proposed the same way from type's full scores (each kind the sum of the types
under it; decided at boundary.KIND_MIN). `propagate(run, fields=…)` writes only some fields (the
boundaries or kind of a run made before them, say).

**The runs chain.** A focused run (talos.focus) is stored as a backfill run is (purpose
'enrich-focus'), and so is an incremental run over new mail (talos.incremental, purpose
'enrich-incremental'). Where a run answers a message's field, the older runs' proposed or
policy-accepted rows of that field become 'superseded' (decided_by 'superseded:<run>'); a rerun
of an older run's proposals keeps them so. The latest run to answer is the one accept sees.

**Accept** (`accept()`), over one run or several: the proposals of a field at or over its
threshold, with a margin of at least ACCEPT_MARGIN (not asked of a boundary: two-sided, its
margin follows), become active, decided_by 'policy:<thresholds>'. It is declarative per field
given: a later accept with other thresholds promotes what now qualifies and puts back to proposed
what an earlier policy accepted and no longer does. The two-level preset (TWO_LEVEL: boundaries
0.90, kind 0.85, origin, type, topic and value 0.70) accepts the boundaries and kind first; an exact
value is then accepted only on the side of the message's effective boundary, or where that is
undecided (held_back counts the rest); a type also only under the message's effective kind. What
was applied is stamped on each run (params.accept).
`unaccept()` puts all of a run's policy-accepted rows back to proposed. Precedence stays as the
views rank it (human > rule > accepted model > import): an accepted model value never beats a
rule or pre-pass value of a one-value field. For ask and route every active value counts, so
accept leaves a proposal proposed when the message or its thread already has a human or rule
value in that field.
"""

from __future__ import annotations

import asyncio
import json
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

import psycopg
from psycopg.types.json import Jsonb

from talos import boundary, gold, jev, mask, policy
from talos.enrich import MACHINE_ORIGINS, NOISE_TYPES, PERSON_ORIGINS, TEAMS_WINDOW_CAP, TEAMS_WINDOW_GAP
from talos.policy import Policy

PURPOSE = "enrich-backfill"
FOCUS_PURPOSE = "enrich-focus"            # talos.focus: one question per case, stored as a backfill run is
INCREMENTAL_PURPOSE = "enrich-incremental"  # talos.incremental: new mail, the full set then kind and value
PURPOSES = (PURPOSE, FOCUS_PURPOSE, INCREMENTAL_PURPOSE)
STAGES = ("machine", "person", "teams")
UNIT = "context"                 # the record design (talos.jev.UNITS); v2 was evaluated on it
FIELDS = gold.FIELDS
MANY = gold.MANY_FIELDS
MAX_CASES = 60_000
CHUNK = 400                      # cases per chunk: built, sent, then stored in one transaction
ESTIMATE_SAMPLE = 200            # records built by a dry run to estimate the size of all
# Measured on an answer key with question templates v2, context unit: 1,776,990 input tokens
# for 300 cases, in 12.1 s
# at 8 in flight (the other v2 run: 11.5–11.9 s).
MEASURED_TOKENS_PER_CASE = 5_923
MEASURED_CASES_PER_SECOND = 26.0
# jev.estimate_tokens (by characters) against the tokens that run was charged, per item: email
# 6,650 estimated vs 6,234 charged per case, Teams 4,416 vs 4,367. The estimate is scaled by these.
CALIBRATION = {"email": 0.937, "teams": 0.989}
# Accept: the per-field thresholds by default (plan §6 table), and the margin every field needs.
ACCEPT_DEFAULTS = {"origin": 0.85, "type": 0.90, "topic": 0.85, "value": 0.85}
ACCEPT_MARGIN = 0.15
# Two-level acceptance (plan §11): the boundaries at 0.90, kind at 0.85, the exact
# values at 0.70 (margin 0.15), an exact value only on its boundary's side (a type under its kind).
ACCEPT_FIELDS = FIELDS + boundary.DERIVED
TWO_LEVEL = {**{b: boundary.BOUNDARY_MIN for b in boundary.FIELDS}, boundary.KIND: boundary.KIND_MIN,
             **{f: boundary.EXACT_MIN for f in ("origin", "type", "topic", "value")}}
PRESETS = {"per-field": ACCEPT_DEFAULTS, "two-level": TWO_LEVEL}
POLICY_PREFIX = "policy:"
# A kind worked out from the full set's type, brought back by accept where the direct kind stayed unsure
# (kind_from_type). Its own prefix: accept's re-evaluation of policy rows leaves it alone.
KIND_FROM_TYPE = "kind-from-type:"
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,120}$")
# An incremental case's key is its unit's key plus "~<anchor id>" when it stands for only the new
# messages of a unit that has an earlier case (talos.incremental). A pattern key ends in its subject
# skeleton, which has no "~" and no digits, so the suffix is unambiguous.
KEY_SUFFIX = re.compile(r"~\d+$")


def base_key(key: str) -> str:
    """The unit's key without an incremental case's "~<anchor>" suffix."""
    return KEY_SUFFIX.sub("", key)
SAME = ("stage", "unit", "question_version", "template_version", "record_version", "mask_version", "policy")


class BackfillError(ValueError):
    pass


# ---------------------------------------------------------------- units

@dataclass
class Case:
    """One case to send: a unit (or one sample of it), how to load its record, and its members."""
    stage: str
    kind: str                    # template | tail | thread | window
    key: str                     # the unit key (enrich_case.unit_key)
    anchor: int
    members: list[int]
    sample: int = 0
    load: dict = field(default_factory=dict)   # gold.unit() arguments: kind and ids
    size: int = 0                # messages the unit stands for

    def unit(self, conn: psycopg.Connection) -> dict:
        u = gold.unit(conn, self.load["kind"], self.anchor, **{k: v for k, v in self.load.items() if k != "kind"})
        u["key"] = self.key
        return u


def _pool_params(since: int | None) -> dict:
    return {"noise": list(NOISE_TYPES), "machine": list(MACHINE_ORIGINS), "person": list(PERSON_ORIGINS),
            "since": since}


def _having(since: int | None) -> str:
    return " having max(received_at) >= now() - make_interval(days => %(since)s)" if since is not None else ""


def _machine(conn: psycopg.Connection, since: int | None) -> list[Case]:
    rows = conn.execute(f"""
        with {gold._ORIGIN_CTE},
        mm as (select m.id, m.received_at, coalesce(mp.pattern_key, 'message:' || m.id) as pk
               from message m left join message_pattern mp on mp.message_id = m.id {gold._JOINS}
               where m.direction = 'in' and m.medium = 'email' and {gold._MACHINE})
        select pk, count(*)::int as n, array_agg(id order by received_at nulls first, id) as ids,
               max(received_at) as latest
        from mm group by pk{_having(since)}
        order by count(*) desc, pk""", _pool_params(since)).fetchall()
    big, tail = [], []
    for r in rows:
        ids, n, pk = r["ids"], r["n"], r["pk"]
        if n >= 3:
            samples = [ids[0], ids[(n + 1) // 2 - 1], ids[-1]]   # first, middle, last (as subject_pattern)
            for s, anchor in enumerate(samples):
                big.append(Case("machine", "template", f"template:{pk}", anchor, ids, s,
                                {"kind": "pattern", "context_ids": [i for i in samples if i != anchor],
                                 "pattern_key": pk}, n))
        else:
            anchor = ids[-1]
            load = ({"kind": "pattern", "context_ids": [i for i in ids if i != anchor], "pattern_key": pk}
                    if n == 2 else {"kind": "message"})
            tail.append((r["latest"], Case("machine", "tail", f"tail:{pk}", anchor, ids, 0, load, n)))
    tail.sort(key=lambda x: (x[0] is None, -(x[0].timestamp()) if x[0] else 0, x[1].key))
    return big + [c for _, c in tail]


def _person(conn: psycopg.Connection, since: int | None) -> list[Case]:
    rows = conn.execute(f"""
        with {gold._ORIGIN_CTE},
        pm as (select m.id, m.thread_id, m.received_at
               from message m {gold._JOINS}
               where m.direction = 'in' and m.medium = 'email' and {gold._PERSON})
        select pm.thread_id, coalesce(pm.thread_id, -min(pm.id)) as tkey,
               array_agg(pm.id order by pm.received_at nulls first, pm.id) as ids, max(pm.received_at) as latest,
               max(t.message_count) as thread_n
        from pm left join thread t on t.id = pm.thread_id
        group by pm.thread_id, case when pm.thread_id is null then pm.id end{_having(since).replace('received_at', 'pm.received_at')}
        order by max(pm.received_at) desc nulls last, 2""", _pool_params(since)).fetchall()
    out = []
    for r in rows:
        ids = r["ids"]
        key = f"thread:{r['thread_id']}" if r["thread_id"] else f"message:{ids[0]}"
        load = {"kind": "thread", "thread_id": r["thread_id"]} if (r["thread_n"] or 0) > 1 else {"kind": "message"}
        out.append(Case("person", "thread", key, ids[-1], ids, 0, load, len(ids)))
    return out


def _teams(conn: psycopg.Connection, since: int | None, threads: list[int] | None = None) -> list[Case]:
    """The answer key's windows (gold._pools): no gap over TEAMS_WINDOW_GAP, split every
    TEAMS_WINDOW_CAP messages. Anchored on the window's latest incoming message (its latest
    message when the owner wrote all of it). threads: only these chats (talos.incremental)."""
    only = " and thread_id = any(%(threads)s)" if threads is not None else ""
    rows = conn.execute(f"""
        with t as (select id, thread_id, received_at, direction,
                          case when received_at - lag(received_at) over (partition by thread_id order by received_at, id)
                                    <= interval '{TEAMS_WINDOW_GAP}' then 0 else 1 end as starts
                   from message where medium <> 'email' and thread_id is not null{only}),
             w as (select *, sum(starts) over (partition by thread_id order by received_at, id rows unbounded preceding) as w
                   from t),
             c as (select *, (row_number() over (partition by thread_id, w order by received_at, id) - 1)
                             / {TEAMS_WINDOW_CAP} as part from w)
        select thread_id, array_agg(id order by received_at, id) as ids,
               coalesce(array_agg(id order by received_at, id) filter (where direction = 'in'), '{{}}') as incoming,
               max(received_at) as latest
        from c group by thread_id, w, part{_having(since)}
        order by max(received_at) desc, thread_id, min(id)""", {"since": since, "threads": threads}).fetchall()
    out = []
    for r in rows:
        ids = r["ids"]
        anchor = r["incoming"][-1] if r["incoming"] else ids[-1]
        out.append(Case("teams", "window", f"window:{r['thread_id']}:{ids[0]}", anchor, ids, 0,
                        {"kind": "window", "thread_id": r["thread_id"], "window_first_id": ids[0],
                         "window_last_id": ids[-1]}, len(ids)))
    return out


SELECT = {"machine": _machine, "person": _person, "teams": _teams}


def cases(conn: psycopg.Connection, stage: str, *, since: int | None = None, limit: int | None = None) -> list[Case]:
    """The stage's cases in sending order (machine: templates by size, then the tail newest
    first; person threads and Teams windows newest first). since keeps the units with a message
    in the last N days; limit keeps the first N units (a template's three samples are one unit).
    Plain SELECTs: works in a read-only transaction."""
    if stage not in STAGES:
        raise BackfillError(f"stage is one of {', '.join(STAGES)}")
    if since is not None and since < 1:
        raise BackfillError("--since is a number of days, at least 1")
    out = SELECT[stage](conn, since)
    if limit is not None:
        keys: list[str] = []
        seen: set[str] = set()
        for c in out:
            if c.key not in seen:
                seen.add(c.key)
                keys.append(c.key)
        keep = set(keys[:max(0, limit)])
        out = [c for c in out if c.key in keep]
    return out


# ---------------------------------------------------------------- the run

def _params(stage: str, qversion: str, pol: Policy, client: jev.JevClient | None) -> dict:
    return {"stage": stage, "unit": UNIT, "question_version": qversion, "template_version": jev.TEMPLATE_VERSION,
            "record_version": jev.RECORD_VERSION, "mask_version": mask.VERSION, "policy": pol.as_dict(),
            "endpoint": client.endpoint if client else jev.ENDPOINT,
            "concurrency": client.concurrency if client else jev.CONCURRENCY,
            "price_per_mtok_input": jev.PRICE_PER_MTOK_INPUT, "tail": "sender+template",
            "cases_done": 0, "failed": 0, "requests": 0, "retries": 0, "input_tokens": 0, "output_tokens": 0,
            "cost_usd": 0.0, "wall_seconds": 0.0, "invocations": 0}


def check_run_id(run_id: str) -> str:
    """A run id is its proposals' source_ref: never one a rule or the pre-pass owns."""
    if not RUN_ID.match(run_id) or run_id.startswith(("rule", "prepass", POLICY_PREFIX.rstrip(":"))):
        raise BackfillError(f"a run id is letters, digits and ._@- and does not start with rule or prepass: {run_id!r}")
    return run_id


def _existing(conn: psycopg.Connection, run_id: str) -> dict | None:
    r = conn.execute("select * from model_run where id = %s", (run_id,)).fetchone()
    if r and r["purpose"] != PURPOSE:
        raise BackfillError(f"run {run_id} is not an enrichment backfill run")
    return r


def _build(conn: psycopg.Connection, batch: list[Case], qsets: dict, model: str,
           per_case: Callable[[Case], tuple[dict, dict]] | None = None) -> list[tuple]:
    """(case, record, body, error) for each case of a batch. per_case gives a case its own
    question sets and dimensions (a combined focused run asks each unit only what it is unsure of)."""
    out = []
    for c in batch:
        try:
            rec = jev.record(c.unit(conn), UNIT)
            qs = per_case(c)[0] if per_case else qsets
            out.append((c, rec, jev.request_body(rec, jev.questions_for(qs, rec), model=model), None))
        except (jev.JevError, psycopg.Error, IndexError, KeyError) as exc:
            out.append((c, None, None, f"record: {type(exc).__name__}: {exc}"))
    return out


def estimate(conn: psycopg.Connection, todo: list[Case], qsets: dict, model: str, *,
             sample: int = ESTIMATE_SAMPLE, concurrency: int = jev.CONCURRENCY) -> dict:
    """Tokens, cost and time for the cases, from the records of an even sample of them."""
    if not todo:
        return {"cases": 0, "input_tokens": 0, "cost_usd": 0.0, "measured_input_tokens": 0,
                "measured_cost_usd": 0.0, "seconds": 0.0, "record_chars": {}, "sampled": 0}
    step = max(1, len(todo) // sample)
    picked = todo[::step][:sample]
    built = [b for b in _build(conn, picked, qsets, model) if b[3] is None]
    per_case = statistics.mean(
        jev.estimate_tokens(b[2]) * CALIBRATION["teams" if b[1].get("unit") == "window" else "email"]
        for b in built) if built else MEASURED_TOKENS_PER_CASE
    tokens = round(per_case * len(todo))
    measured = MEASURED_TOKENS_PER_CASE * len(todo)
    rate = MEASURED_CASES_PER_SECOND * concurrency / jev.CONCURRENCY
    sizes = [jev.record_chars(b[1]) for b in built]
    return {"cases": len(todo), "sampled": len(built), "input_tokens": tokens, "input_tokens_per_case": round(per_case),
            "cost_usd": round(jev.cost(tokens), 4), "measured_input_tokens": measured,
            "measured_cost_usd": round(jev.cost(measured), 4), "seconds": round(len(todo) / rate, 1),
            "record_chars": {"min": min(sizes), "median": int(statistics.median(sizes)), "max": max(sizes)} if sizes else {},
            "example": built[0] if built else None}


def _summary_units(todo: list[Case]) -> dict:
    kinds: dict[str, dict] = {}
    seen: set[str] = set()
    for c in todo:
        k = kinds.setdefault(c.kind, {"units": 0, "cases": 0, "messages": 0})
        k["cases"] += 1
        if c.key not in seen:
            seen.add(c.key)
            k["units"] += 1
            k["messages"] += c.size
    return kinds


def _plan(conn: psycopg.Connection, stage: str, run_id: str | None, since: int | None, limit: int | None,
          pol: Policy, client: jev.JevClient | None) -> tuple:
    """What a run would send: the summary (counts, estimate, one example request) and the cases."""
    dims = jev.taxonomy_from_db(conn)
    qsets = jev.question_sets(dims)
    qversion = jev.question_version(qsets)
    model = client.model if client else jev.MODEL
    concurrency = client.concurrency if client else jev.CONCURRENCY
    existing = _existing(conn, run_id) if run_id else None
    done: set[tuple[str, int]] = set()
    if existing:
        want = _params(stage, qversion, pol, client)
        diff = [k for k in SAME if existing["params"].get(k) != want[k]]
        if diff:
            raise BackfillError(f"run {run_id} was made with another {', '.join(diff)}; start a new run instead")
        done = {(r["unit_key"], r["sample_no"]) for r in conn.execute(
            "select unit_key, sample_no from enrich_case where run_id = %s and error is null", (run_id,))}
    t_select = time.monotonic()
    planned = cases(conn, stage, since=since, limit=limit)
    todo = [c for c in planned if (c.key, c.sample) not in done]
    select_seconds = round(time.monotonic() - t_select, 2)
    est = estimate(conn, todo, qsets, model, concurrency=concurrency)
    example = est.pop("example", None)
    summary = {"stage": stage, "run_id": run_id, "model": model, "question_version": qversion,
               "template_version": jev.TEMPLATE_VERSION, "since": since, "limit": limit, "planned": len(planned),
               "already_done": len(planned) - len(todo), "to_send": len(todo), "units": _summary_units(todo),
               "estimate": est, "worst_cost_usd": max(est["cost_usd"], est["measured_cost_usd"]),
               "select_seconds": select_seconds, "concurrency": concurrency,
               "request": example[2] if example else None,
               "example_unit": {"key": example[0].key, "anchor": example[0].anchor, "kind": example[0].kind}
               if example else None}
    return summary, planned, todo, existing, dims, qsets, qversion, model


def run(conn: psycopg.Connection, stage: str, *, client: jev.JevClient | None = None, run_id: str | None = None,
        since: int | None = None, limit: int | None = None, max_cases: int = MAX_CASES, budget: float | None = None,
        dry_run: bool = False, pol: Policy = policy.DEFAULT, chunk: int = CHUNK,
        progress: Callable[[str], None] = print) -> dict:
    """Run one stage of the backfill (or resume run_id), then write its proposals; with dry_run
    only count, estimate and show one request. Returns a summary. See the module docstring."""
    if stage not in STAGES:
        raise BackfillError(f"stage is one of {', '.join(STAGES)}")
    if run_id:
        check_run_id(run_id)
    if dry_run:
        # A dry run writes nothing, and proves it: on a connection with no transaction open it
        # works in a read-only one (the real archive can be sized this way).
        idle = conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        with conn.transaction():
            if idle:
                conn.execute("set transaction read only")
            summary, *_ = _plan(conn, stage, run_id, since, limit, pol, client)
        summary["over_max_cases"] = summary["to_send"] > max_cases
        summary["over_budget"] = budget is not None and summary["worst_cost_usd"] > budget
        return summary
    summary, planned, todo, existing, dims, qsets, qversion, model = _plan(conn, stage, run_id, since, limit, pol, client)
    est, worst = summary["estimate"], summary["worst_cost_usd"]
    if len(todo) > max_cases:
        raise BackfillError(f"refused: {len(todo):,} cases to send is over --max-cases {max_cases:,}"
                            f" (about {est['measured_input_tokens']:,} input tokens, ${worst:.2f});"
                            " raise it, or use --limit or --since")
    if budget is not None and worst > budget:
        raise BackfillError(f"refused: the estimate ${worst:.2f} for {len(todo):,} cases is over --budget ${budget:.2f};"
                            " raise it, or use --limit or --since")
    summary.pop("request", None)
    if client is None:
        client = jev.JevClient()
    if not existing:
        run_id = run_id or f"jev-backfill-{stage}-{qversion[:8]}-{jev.now_stamp()}"
        check_run_id(run_id)
        conn.execute("insert into model_run (id, model, purpose, params, input_count) values (%s, %s, %s, %s, %s)",
                     (run_id, model, PURPOSE, Jsonb(_params(stage, qversion, pol, client)), len(planned)))
    else:
        conn.execute("update model_run set input_count = greatest(input_count, %s) where id = %s", (len(planned), run_id))
    if not conn.autocommit:
        conn.commit()
    summary["run_id"] = run_id
    total = len(todo)
    progress(f"backfill {stage}: run {run_id}: {total:,} cases to send ({len(planned) - total:,} already done),"
             f" {client.concurrency} in flight, about {est['measured_input_tokens']:,} input tokens (${worst:.2f}),"
             f" about {est['seconds'] / 60:.0f} min")
    state, before = _send(conn, run_id, f"backfill {stage}", todo, client, qsets, dims, pol, model, chunk=chunk, budget=budget,
                          progress=progress)
    summary.update(stored=state["stored"], failed=[{"unit": k, "error": e} for k, e in state["failed"]],
                   usage={k: client.usage.as_dict()[k] - before[k] for k in before},
                   seconds=round(time.monotonic() - state["t0"], 2))
    summary["cost_usd"] = round(jev.cost(summary["usage"]["input_tokens"]), 6)
    if state["stopped"]:
        summary["error"] = state["stopped"]
    t = time.monotonic()
    summary["propagation"] = propagate(conn, run_id)
    summary["propagation"]["seconds"] = round(time.monotonic() - t, 2)
    return summary


def _send(conn: psycopg.Connection, run_id: str, label: str, todo: list[Case], client: jev.JevClient, qsets: dict,
          dims: dict, pol: Policy, model: str, *, chunk: int = CHUNK, budget: float | None = None,
          progress: Callable[[str], None] = print, context: dict | None = None,
          per_case: Callable[[Case], tuple[dict, dict]] | None = None) -> tuple[dict, dict]:
    """Send the cases in chunks and store each chunk's answers (a backfill stage, or a focused
    run: talos.focus). Stops once the budget is spent, or at a refused key. Returns the state
    (stored, failed, t0, stopped) and the client's usage before."""
    total = len(todo)
    state = {"stored": 0, "failed": [], "t0": time.monotonic(), "stopped": None}
    before = client.usage.as_dict()
    batches = [todo[i:i + chunk] for i in range(0, len(todo), chunk)]
    try:
        # While a chunk is out, the next chunk's records are built in a worker thread; the
        # connection is idle then (answers are only collected), so it has one user at a time.
        with ThreadPoolExecutor(max_workers=1) as pool:
            built = _build(conn, batches[0], qsets, model, per_case) if batches else []
            for k in range(len(batches)):
                sendable = [(i, b[2]) for i, b in enumerate(built) if b[3] is None]
                results: dict[int, jev.Result] = {}
                nxt = pool.submit(_build, conn, batches[k + 1], qsets, model, per_case) if k + 1 < len(batches) \
                    else None
                try:
                    asyncio.run(client.run(sendable, lambda r: results.__setitem__(r.case_id, r)))
                finally:
                    following = nxt.result() if nxt is not None else []
                    stored, failed = _store_chunk(conn, run_id, built, results, dims, pol, model, context, per_case)
                    state["stored"] += stored
                    state["failed"] += failed
                built = following
                n = state["stored"] + len(state["failed"])
                spent = jev.cost(client.usage.input_tokens - before["input_tokens"])
                elapsed = time.monotonic() - state["t0"]
                rate = n / max(0.001, elapsed)
                progress(f"{label}: {n:,}/{total:,} ({state['stored']:,} stored, {len(state['failed']):,} failed),"
                         f" {elapsed:.0f} s, {rate:.1f} cases/s,"
                         f" {client.usage.input_tokens - before['input_tokens']:,} input tokens (${spent:.2f}),"
                         f" about {(total - n) / max(rate, 0.001) / 60:.0f} min left")
                if budget is not None and spent >= budget and k + 1 < len(batches):
                    state["stopped"] = f"the budget ${budget:.2f} is spent (${spent:.2f}); resume with --run {run_id}"
                    break
    except jev.JevAuthError as exc:
        state["stopped"] = str(exc)
    finally:
        _account(conn, run_id, client, before, time.monotonic() - state["t0"])
    return state, before


def _top3(scores: dict) -> dict:
    return dict(sorted(((v, round(float(p), 4)) for v, p in scores.items()), key=lambda x: (-x[1], x[0]))[:3])


def _store_chunk(conn: psycopg.Connection, run_id: str, built: list[tuple], results: dict,
                 dims: dict, pol: Policy, model: str, context: dict | None = None,
                 per_case: Callable[[Case], tuple[dict, dict]] | None = None) -> tuple[int, list]:
    """A chunk's cases and predictions in one transaction. A failed case is stored with its
    error (and replaces an earlier failure of the same unit); a resumed run tries it again.

    dims are the fields asked (all six in a backfill, one in a focused run, the case's own in a
    combined one: per_case). context gives, per
    (unit key, sample), other fields' scores the gates need (a focused ask or route run routes with
    the origin and topic its source case had); only the fields asked are stored."""
    case_rows, preds, failed = [], [], []
    for i, (c, rec, _body, err) in enumerate(built):
        res = results.get(i)
        fields = None
        if err is None and res is None:
            err = "not answered (the run stopped)"
        elif err is None and res.error is not None:
            err = res.error
        elif err is None:
            try:
                fields = jev.read_answers(per_case(c)[1] if per_case else dims, res.response.get("answers"),
                                          teams=rec.get("unit") == "window")
            except jev.JevError as exc:
                err = str(exc)
        if err is not None:
            failed.append((c.key, err))
        usage = (res.usage if res is not None else {}) or {}
        case_rows.append((c, rec, res, err, fields, usage))
    stored = 0
    with conn.transaction():
        for c, rec, res, err, fields, usage in case_rows:
            conn.execute("delete from enrich_case where run_id = %s and unit_key = %s and sample_no = %s"
                         " and error is not null", (run_id, c.key, c.sample))
            cid = conn.execute(
                "insert into enrich_case (run_id, stage, unit_kind, unit_key, sample_no, anchor_id, member_ids,"
                " record_sha256, record_chars, model, input_tokens, output_tokens, elapsed_ms, error)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (run_id, unit_key, sample_no) do nothing returning id",
                (run_id, c.stage, c.kind, c.key, c.sample, c.anchor, c.members, jev.record_sha(rec) if rec else None,
                 jev.record_chars(rec) if rec else None,
                 (res.response or {}).get("model") or model if res is not None and res.response else None,
                 usage.get("input_tokens", 0), usage.get("output_tokens", 0),
                 int(res.seconds * 1000) if res is not None else None, err)).fetchone()
            if cid is None or fields is None:
                continue
            stored += 1
            asked = {f: a["scores"] for f, a in fields.items()}
            decisions, fired = policy.route_case({**((context or {}).get((c.key, c.sample)) or {}), **asked}, pol,
                                                 template=jev.TEMPLATE_VERSION)
            for f, a in fields.items():
                d = decisions[f]
                ungated = list(policy.route_choice_many(f, a["scores"], pol, policy.CHOICE_MANY[f]).selected) \
                    if f in fired else None
                fixed = isinstance(a["raw"], dict) and "fixed" in a["raw"]
                preds.append((cid["id"], f, d.top, list(d.selected), Jsonb(a["scores"]), Jsonb(_top3(a["scores"])),
                              d.confidence, d.margin, d.decided, fired.get(f, []), ungated, fixed))
        if preds:
            with conn.cursor() as cur:
                cur.executemany(
                    "insert into enrich_prediction (case_id, field, top, selected, scores, top3, confidence, margin,"
                    " decided, gate, ungated, fixed) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", preds)
    return stored, failed


def _account(conn: psycopg.Connection, run_id: str, client: jev.JevClient, before: dict, seconds: float) -> None:
    """Add this invocation's usage, cost and time to the run's totals."""
    row = conn.execute("select params from model_run where id = %s", (run_id,)).fetchone()
    p = dict(row["params"])
    now = client.usage.as_dict()
    for k in ("requests", "retries", "input_tokens", "output_tokens"):
        p[k] = int(p.get(k) or 0) + now[k] - before[k]
    p["cost_usd"] = round(jev.cost(p["input_tokens"], p.get("price_per_mtok_input", jev.PRICE_PER_MTOK_INPUT)), 6)
    p["wall_seconds"] = round(float(p.get("wall_seconds") or 0) + seconds, 3)
    p["invocations"] = int(p.get("invocations") or 0) + 1
    c = conn.execute("select count(*) filter (where error is null)::int as done,"
                     " count(*) filter (where error is not null)::int as failed from enrich_case where run_id = %s",
                     (run_id,)).fetchone()
    p["cases_done"], p["failed"] = c["done"], c["failed"]
    conn.execute("update model_run set params = %s, output_count = %s where id = %s", (Jsonb(p), c["done"], run_id))
    if not conn.autocommit:
        conn.commit()


def format_dry_run(s: dict) -> str:
    e = s["estimate"]
    lines = ["DRY RUN: nothing is sent, no key is read, nothing is stored.",
             f"Stage {s['stage']}, model {s['model']}, question templates v{s['template_version']},"
             f" question version {s['question_version']}, unit design {UNIT}"
             + (f", units with a message in the last {s['since']} days" if s["since"] else "")
             + (f", the first {s['limit']} units" if s["limit"] is not None else ""),
             f"{s['to_send']:,} cases would be sent" + (f" ({s['already_done']:,} already done)" if s["already_done"] else "")
             + f" (selected in {s['select_seconds']} s):"]
    for k, u in s["units"].items():
        lines.append(f"  {k:<9} {u['units']:>7,} units  {u['cases']:>7,} cases  standing for {u['messages']:>8,} messages")
    lines += [f"record size in characters (a sample of {e['sampled']}): {e['record_chars']}",
              f"estimated input tokens: {e['input_tokens']:,} by size, calibrated ({e.get('input_tokens_per_case', 0):,} per case,"
              f" ${e['cost_usd']:.2f}); {e['measured_input_tokens']:,} at the measured"
              f" {MEASURED_TOKENS_PER_CASE:,} per case (${e['measured_cost_usd']:.2f});"
              f" ${jev.PRICE_PER_MTOK_INPUT} per million input tokens, output free",
              f"estimated time: {e['seconds'] / 60:.1f} min at {MEASURED_CASES_PER_SECOND:g} cases/s per 8 in flight"
              f" ({s['concurrency']} in flight), plus the proposals"]
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


# ---------------------------------------------------------------- proposals

def _run(conn: psycopg.Connection, run_id: str) -> dict:
    r = conn.execute("select * from model_run where id = %s", (run_id,)).fetchone()
    if not r or r["purpose"] not in PURPOSES:
        raise BackfillError(f"no enrichment backfill run {run_id!r}")
    return r


def runs_of(conn: psycopg.Connection, spec: str | list[str]) -> list[str]:
    """Run ids from a --run value: 'all' is every backfill and focused run, oldest first; otherwise
    a comma-separated list (or a list) of ids, each checked."""
    if isinstance(spec, str) and spec.strip() == "all":
        return [r["id"] for r in conn.execute("select id from model_run where purpose = any(%s) order by created_at, id",
                                              (list(PURPOSES),))]
    ids = [x.strip() for x in (spec.split(",") if isinstance(spec, str) else spec) if x and x.strip()]
    if not ids:
        raise BackfillError("name a run (--run RUN, several comma-separated, or all)")
    for r in ids:
        _run(conn, r)
    return list(dict.fromkeys(ids))


_MANY_SQL = "array[" + ", ".join(f"'{f}'" for f in MANY) + "]"


def _boundary_sides(conn: psycopg.Connection) -> dict | None:
    try:
        return boundary.sides(conn)
    except boundary.BoundaryError:
        return None  # the taxonomy has no boundaries yet: proposals for the six fields only


def propagate(conn: psycopg.Connection, run_id: str, *, fields=None) -> dict:
    """Write the run's answers as proposed assignments on messages (module docstring), in one
    transaction: the run's proposed rows are replaced; accepted or rejected ones are kept.

    Besides the six fields, the four boundaries and kind (talos.boundary) are proposed, derived from
    the stored full scores (or asked directly, in a focused run). fields limits the work to those
    fields: the others' rows are not touched.

    A newer run supersedes an older one (the runs chain): where this run answers a field of a
    message, the older runs' proposed or policy-accepted rows of that field get status
    'superseded' (decided_by 'superseded:<run>'), and rows this writes where a newer run already
    answers are superseded at once. So among the backfill and focused runs, the latest one that
    answered a message's field is the only one that can be accepted for it."""
    run = _run(conn, run_id)
    qv = run["params"].get("question_version")
    sd = _boundary_sides(conn)
    known = FIELDS + (tuple(b for b in boundary.DERIVED if b in sd) if sd else ())
    fields = tuple(fields) if fields else known
    bad = [f for f in fields if f not in known]
    if bad:
        raise BackfillError(f"cannot propose {', '.join(bad)}" + ("" if sd else " (load the taxonomy first)"))
    p = {"run": run_id, "runs": [run_id], "qv": qv, "fields": list(fields), "bmin": boundary.BOUNDARY_MIN - 1e-9,
         "kmin": boundary.KIND_MIN - 1e-9, "purposes": list(PURPOSES),
         **(boundary.params(sd) if sd else {"vs_b": [], "vs_f": [], "vs_v": [], "vs_s": []})}
    with conn.transaction():
        conn.execute("drop table if exists bf_pred, bf_agree, bf_rows, bf_kept, bf_cover")
        # Every answered field, with the values it proposes and its signature for agreement; the
        # boundaries derived from the stored scores beside the stored predictions.
        conn.execute(f"""
            create temp table bf_pred on commit drop as
            select x.*,
                   case when x.field = any({_MANY_SQL}) then x.selected
                        when x.top is null then '{{}}'::text[] else array[x.top] end as vals,
                   case when x.field = any({_MANY_SQL})
                        then array_to_string(array(select v from unnest(x.selected) v order by v), ',')
                        else coalesce(x.top, '') end as sig
            from (select c.id as case_id, c.unit_kind, c.unit_key, c.sample_no, c.anchor_id, c.member_ids,
                         p.field, p.top, p.selected, p.top3, p.confidence, p.margin, p.decided, p.gate,
                         null::text as derived_from, false as fixed, c.source
                  from enrich_case c join enrich_prediction p on p.case_id = c.id
                  where c.run_id = %(run)s and c.error is null and not p.fixed and p.field = any(%(fields)s)
                  union all
                  select c.id, c.unit_kind, c.unit_key, c.sample_no, c.anchor_id, c.member_ids,
                         d.field, d.top, case when d.confidence >= case when d.field = 'kind' then %(kmin)s
                                                                        else %(bmin)s end then array[d.top] else '{{}}' end,
                         d.scores, d.confidence, d.margin,
                         d.confidence >= case when d.field = 'kind' then %(kmin)s else %(bmin)s end, '{{}}'::text[],
                         d.derived_from, d.fixed, c.source
                  from ({boundary.DERIVED_SQL}) d join enrich_case c on c.id = d.case_id
                  where d.field = any(%(fields)s)) x""", p)
        conn.execute("create index on bf_pred (case_id, field)")
        # Templates whose three samples were all answered and agree, per field, with the lowest
        # confidence and margin of the three (what their other messages get).
        conn.execute("""
            create temp table bf_agree on commit drop as
            select unit_key, field, array_agg(case_id order by sample_no) as cases,
                   array_agg(anchor_id) as anchors,
                   (array_agg(case_id order by confidence nulls first, sample_no))[1] as weakest,
                   min(confidence) as conf, min(margin) as margin, bool_and(decided) as decided
            from bf_pred where unit_kind = 'template'
            group by unit_key, field
            having count(*) = 3 and count(distinct sig) = 1""")
        conn.execute("create index on bf_agree (unit_key, field)")
        conn.execute("analyze bf_pred; analyze bf_agree")
        conn.execute("""
            create temp table bf_rows on commit drop as
            -- every case's anchor: its own answer
            select b.anchor_id as entity_id, b.field, v.value, b.confidence, b.margin, b.decided, b.gate, b.top3,
                   b.case_id, null::bigint[] as cases, b.unit_key as unit, false as propagated,
                   case when b.unit_kind = 'template' then a.unit_key is not null end as agree,
                   b.derived_from, b.fixed, b.source
            from bf_pred b cross join lateral unnest(b.vals) v(value)
            left join bf_agree a on a.unit_key = b.unit_key and a.field = b.field
            union all
            -- a tail, thread or window case: its other members
            select m.id, b.field, v.value, b.confidence, b.margin, b.decided, b.gate, b.top3,
                   b.case_id, null, b.unit_key, true, null, b.derived_from, b.fixed, b.source
            from bf_pred b cross join lateral unnest(b.vals) v(value)
            cross join lateral unnest(b.member_ids) m(id)
            where b.unit_kind <> 'template' and m.id <> b.anchor_id
            union all
            -- an agreeing template: its other messages, with the weakest sample's numbers
            select m.id, a.field, v.value, a.conf, a.margin, a.decided, w.gate, w.top3,
                   w.case_id, a.cases, a.unit_key, true, true, w.derived_from, w.fixed, w.source
            from bf_agree a join bf_pred w on w.case_id = a.weakest and w.field = a.field
            cross join lateral unnest(w.vals) v(value)
            cross join lateral unnest(w.member_ids) m(id)
            where not (m.id = any(a.anchors))""")
        # What the run answers, per message and field, whatever it proposes (an ask of none too):
        # what it supersedes in older runs.
        conn.execute("""
            create temp table bf_cover on commit drop as
            select anchor_id as entity_id, field from bf_pred
            union
            select m.id, b.field from bf_pred b cross join lateral unnest(b.member_ids) m(id)
            where b.unit_kind <> 'template'
            union
            select m.id, a.field from bf_agree a join bf_pred w on w.case_id = a.weakest and w.field = a.field
            cross join lateral unnest(w.member_ids) m(id)""")
        replaced = conn.execute("delete from assignment where source_kind = 'model' and source_ref = %(run)s"
                                " and status = 'proposed' and dimension_id = any(%(fields)s)", p).rowcount
        # What the run keeps (accepted, rejected or superseded rows) is not proposed again.
        conn.execute("create temp table bf_kept on commit drop as select entity_id, dimension_id, value from assignment"
                     " where source_kind = 'model' and source_ref = %(run)s and dimension_id = any(%(fields)s)", p)
        conn.execute("analyze bf_rows; analyze bf_kept; analyze bf_cover")
        written = conn.execute("""
            insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, status, confidence, evidence)
            select distinct on (r.entity_id, r.field, r.value)
                   r.entity_id, r.field, r.value, 'model', %(run)s, 'proposed', r.confidence,
                   jsonb_strip_nulls(jsonb_build_object(
                       'case', r.case_id, 'cases', to_jsonb(r.cases), 'unit', r.unit,
                       'propagated', r.propagated or r.source is not null, 'propagated_from', r.source,
                       'agree', r.agree, 'scores', r.top3, 'margin', r.margin, 'decided', r.decided,
                       'gate', case when cardinality(r.gate) > 0 then to_jsonb(r.gate) end,
                       'boundary_of', r.derived_from, 'fixed', case when r.fixed then true end,
                       'question_version', %(qv)s::text))
            from bf_rows r
            where not exists (select 1 from bf_kept k where k.entity_id = r.entity_id and k.dimension_id = r.field
                              and k.value = r.value)
            order by r.entity_id, r.field, r.value, r.propagated, r.confidence desc nulls last""", p).rowcount
        # The runs chain: older runs' rows of what this run answers are superseded ...
        older = ("select id from model_run where purpose = any(%(purposes)s) and id <> %(run)s"
                 " and (created_at, id) < (select created_at, id from model_run where id = %(run)s)")
        superseded = conn.execute(f"""
            update assignment a set status = 'superseded', decided_by = 'superseded:' || %(run)s, decided_at = now()
            from bf_cover v
            where a.entity_id = v.entity_id and a.dimension_id = v.field and a.source_kind = 'model'
              and a.source_ref in ({older})
              and (a.status = 'proposed' or (a.status = 'active' and (a.decided_by like '{POLICY_PREFIX}%%'
                                                                   or a.decided_by like '{KIND_FROM_TYPE}%%')))""",
                                  p).rowcount
        # ... and what this wrote where a newer run already answers (a rerun of an older run) is too.
        newer = older.replace(") < (", ") > (")
        superseded_here = conn.execute(f"""
            update assignment a set status = 'superseded', decided_by = 'superseded:' || n.source_ref,
                   decided_at = now()
            from (select distinct on (entity_id, dimension_id) entity_id, dimension_id, source_ref from assignment
                  where source_kind = 'model' and source_ref in ({newer}) and dimension_id = any(%(fields)s)
                  order by entity_id, dimension_id, created_at desc, id desc) n
            where a.source_kind = 'model' and a.source_ref = %(run)s and a.status = 'proposed'
              and a.entity_id = n.entity_id and a.dimension_id = n.dimension_id""", p).rowcount
        by_field = {r["field"]: r["n"] for r in conn.execute(
            "select field, count(*)::int as n from bf_rows group by 1 order by 1")}
        tpl = conn.execute("""
            select field, count(*) filter (where agree)::int as agreed, count(*) filter (where not agree)::int as split
            from (select b.unit_key, b.field, bool_or(a.unit_key is not null) as agree
                  from bf_pred b left join bf_agree a on a.unit_key = b.unit_key and a.field = b.field
                  where b.unit_kind = 'template' group by b.unit_key, b.field) x group by field order by field""").fetchall()
    return {"replaced": replaced, "written": written, "rows_by_field": by_field, "superseded": superseded,
            "superseded_here": superseded_here,
            "templates": {r["field"]: {"agreed": r["agreed"], "split": r["split"]} for r in tpl}}


# ---------------------------------------------------------------- accept

def policy_label(thresholds: dict[str, float], margin: float) -> str:
    return POLICY_PREFIX + ",".join(f"{f}>={thresholds[f]:.2f}" for f in ACCEPT_FIELDS if f in thresholds) \
        + f",margin>={margin:.2f}"


def _agreement_tables(conn: psycopg.Connection, sd: dict) -> None:
    """acc_vs: each exact value's boundary (and a type's kind) and side; acc_eff: each message's
    effective boundary side and kind (human > rule > accepted model > import; the message's own
    before its thread's; then the newest), as it stands after the boundaries and kind were accepted."""
    conn.execute("drop table if exists acc_vs, acc_eff")
    conn.execute("create temp table acc_vs on commit drop as select * from unnest(%(vs_b)s::text[], %(vs_f)s::text[],"
                 " %(vs_v)s::text[], %(vs_s)s::text[]) as t(bdim, field, value, side)", boundary.params(sd))
    conn.execute("""
        create temp table acc_eff on commit drop as
        select distinct on (x.message_id, x.bdim) x.message_id, x.bdim, x.side from (
            select a.entity_id as message_id, a.dimension_id as bdim, a.value as side, a.source_kind, a.created_at,
                   a.id, true as own
            from assignment a where a.status = 'active' and a.dimension_id = any(%(b)s)
            union all
            select m.id, a.dimension_id, a.value, a.source_kind, a.created_at, a.id, false
            from assignment a join message m on m.thread_id = a.entity_id
            where a.status = 'active' and a.dimension_id = any(%(b)s)) x
        order by x.message_id, x.bdim,
                 case x.source_kind when 'human' then 4 when 'rule' then 3 when 'model' then 2 else 1 end desc,
                 x.own desc, x.created_at desc, x.id desc""", {"b": [b for b in boundary.DERIVED if b in sd]})
    conn.execute("create index on acc_eff (message_id, bdim)")
    conn.execute("analyze acc_vs; analyze acc_eff")


_DISAGREE = ("exists (select 1 from acc_vs v join acc_eff e on e.bdim = v.bdim and e.side <> v.side"
             " where v.field = a.dimension_id and v.value = a.value and e.message_id = a.entity_id)")


def _accept_field(conn: psycopg.Connection, runs: list[str], f: str, t: float, margin: float, label: str,
                  agree: bool) -> dict:
    p = {"runs": runs, "f": f, "t": t - 1e-6, "m": margin - 1e-6, "label": label, "pp": POLICY_PREFIX + "%"}
    over = "a.confidence >= %(t)s"
    if f not in boundary.DERIVED:  # a derived field's margin follows from its probability (≥ 0.5 leads)
        over += " and coalesce((a.evidence ->> 'margin')::float, 0) >= %(m)s"
    ok = over
    if f in MANY:  # every active value counts: never beside a human's or a rule's own value
        ok += (" and not exists (select 1 from message msg join assignment h on h.entity_id in (msg.id, msg.thread_id)"
               " where msg.id = a.entity_id and h.dimension_id = a.dimension_id and h.status = 'active'"
               " and h.source_kind in ('human', 'rule'))")
    if agree:  # two-level: an exact value only on the side of the message's boundary, or none decided
        ok += f" and not {_DISAGREE}"
    base = "a.source_kind = 'model' and a.source_ref = any(%(runs)s) and a.dimension_id = %(f)s"
    demoted = conn.execute(
        f"update assignment a set status = 'proposed', decided_by = null, decided_at = null"
        f" where {base} and a.status = 'active' and a.decided_by like %(pp)s and not ({ok})", p).rowcount
    kept = conn.execute(
        f"update assignment a set decided_by = %(label)s, decided_at = now()"
        f" where {base} and a.status = 'active' and a.decided_by like %(pp)s and a.decided_by <> %(label)s"
        f" and ({ok})", p).rowcount
    values = conn.execute(
        f"with u as (update assignment a set status = 'active', decided_by = %(label)s, decided_at = now()"
        f" where {base} and a.status = 'proposed' and ({ok}) returning a.value)"
        f" select value, count(*)::int as n from u group by 1 order by 2 desc, 1", p).fetchall()
    counts = conn.execute(
        "select count(*) filter (where a.status = 'proposed')::int as left,"
        " count(*) filter (where a.status = 'active')::int as active"
        + (f", count(*) filter (where a.status = 'proposed' and {over} and {_DISAGREE})::int as held" if agree else "")
        + f" from assignment a where {base} and a.status in ('proposed', 'active')", p).fetchone()
    return {"threshold": t, "promoted": sum(v["n"] for v in values), "demoted": demoted, "restamped": kept,
            "active": counts["active"], "still_proposed": counts["left"], "held_back": counts.get("held", 0),
            "promoted_values": {v["value"]: v["n"] for v in values}}


def _kind_from_type(conn: psycopg.Connection, runs: list[str], t: float) -> dict:
    """Kind worked out from the full set's type, where the direct kind stayed unsure.

    On answer key 1 (277 items labelled through their type), kind derived from the full question
    set's type scores decided 203 at ≥ 0.85 and was right on 94.6%; kind asked directly decided 159
    and was right on 89.3%. But the direct runs are newer, so the runs chain superseded 220,000
    derived answers with less sure direct ones. So after kind is accepted: a message with no kind
    that counts gets its newest derived kind back (status active, decided_by 'kind-from-type:…',
    the superseder kept in evidence.superseded_by) when that one is at the kind level. Earlier ones
    now under the level go back to superseded. A newer run supersedes them as it does policy rows."""
    p = {"runs": runs, "t": t - 1e-6, "label": f"{KIND_FROM_TYPE}kind>={t:.2f}", "pre": KIND_FROM_TYPE + "%"}
    back = conn.execute(
        "update assignment set status = 'superseded', decided_by = coalesce(evidence->>'superseded_by', 'superseded'),"
        " decided_at = now(), evidence = evidence - 'superseded_by'"
        " where source_kind = 'model' and source_ref = any(%(runs)s) and dimension_id = 'kind' and status = 'active'"
        " and decided_by like %(pre)s and confidence < %(t)s", p).rowcount
    rows = conn.execute("""
        with newest as (
            select distinct on (a.entity_id) a.id, a.entity_id, a.status, a.confidence
            from assignment a
            where a.dimension_id = 'kind' and a.source_kind = 'model' and a.source_ref = any(%(runs)s)
              and a.evidence ->> 'boundary_of' = 'type' and a.status in ('superseded', 'proposed', 'active')
            order by a.entity_id, a.created_at desc, a.id desc),
        u as (
            update assignment a set status = 'active', decided_by = %(label)s, decided_at = now(),
                   evidence = a.evidence || jsonb_build_object('superseded_by', a.decided_by)
            from newest n join message m on m.id = n.entity_id
            where a.id = n.id and n.status = 'superseded' and n.confidence >= %(t)s
              and not exists (select 1 from assignment b where b.entity_id in (m.id, m.thread_id)
                              and b.dimension_id = 'kind' and b.status = 'active')
            returning a.value)
        select value, count(*)::int as n from u group by 1 order by 2 desc, 1""", p).fetchall()
    return {"threshold": t, "brought_back": sum(r["n"] for r in rows), "returned": back,
            "values": {r["value"]: r["n"] for r in rows}}


def accept(conn: psycopg.Connection, run_id: str | list[str], thresholds: dict[str, float], *,
           margin: float = ACCEPT_MARGIN, dry_run: bool = False) -> dict:
    """Make the runs' proposals of each field in thresholds active where confidence ≥ its
    threshold (and, for all but the boundaries, margin ≥ margin); put back to proposed what an
    earlier policy accepted and this one does not. Fields not in thresholds are left as they are.
    Returns per-field counts over the runs (and per value what is promoted). dry_run: the same,
    rolled back.

    Two-level (docs/enrichment-plan.md §11): the boundaries and kind are accepted first; an exact
    value (origin, type, topic, value) is then accepted only when its side agrees with the message's
    effective boundary, or the boundary is undecided (held_back counts those it keeps proposed); a
    type also only under the message's effective kind, where one is decided. A run with no
    boundary or kind proposals yet gets them first (propagate, those fields only). Where the direct
    kind stays unsure, the kind worked out from type comes back (_kind_from_type)."""
    runs = [run_id] if isinstance(run_id, str) else list(run_id)
    if not runs:
        raise BackfillError("name a run")
    for r in runs:
        _run(conn, r)
    bad = [f for f in thresholds if f not in ACCEPT_FIELDS]
    if bad:
        raise BackfillError(f"no field {', '.join(bad)}; the fields are {', '.join(ACCEPT_FIELDS)}")
    for f, t in thresholds.items():
        if not 0.0 <= t <= 1.0:
            raise BackfillError(f"the threshold of {f} is a probability from 0 to 1")
    label = policy_label(thresholds, margin)
    out: dict = {"run_id": ",".join(runs), "runs": runs, "decided_by": label, "dry_run": dry_run, "fields": {},
                 "boundaries_written": {}}
    want_b = [b for b in boundary.DERIVED if b in thresholds]
    with conn.transaction():
        sd = _boundary_sides(conn)
        if want_b and (sd is None or any(b not in sd for b in want_b)):
            raise BackfillError(f"{', '.join(b for b in want_b if not sd or b not in sd)} has no mapping yet;"
                                " load the taxonomy first: talos taxonomy load")
        for r in runs:  # a run from before the boundaries: derive its boundary proposals first
            missing = [b for b in want_b if conn.execute(
                "select 1 from assignment where source_kind = 'model' and source_ref = %s and dimension_id = %s limit 1",
                (r, b)).fetchone() is None]
            if missing:
                out["boundaries_written"][r] = propagate(conn, r, fields=missing)["written"]
        for f in want_b:
            out["fields"][f] = _accept_field(conn, runs, f, thresholds[f], margin, label, False)
        if boundary.KIND in thresholds:  # before the exact fields: a type is accepted under the effective kind
            out["kind_from_type"] = _kind_from_type(conn, runs, thresholds[boundary.KIND])
        exact = [f for f in FIELDS if f in thresholds and f not in MANY]
        if sd is not None and any(f in boundary.OF for f in exact):
            _agreement_tables(conn, sd)
        for f in exact:
            out["fields"][f] = _accept_field(conn, runs, f, thresholds[f], margin, label,
                                             sd is not None and f in boundary.OF)
        for f in [f for f in MANY if f in thresholds]:
            out["fields"][f] = _accept_field(conn, runs, f, thresholds[f], margin, label, False)
        # What is applied now, per run (the acceptance explorer shows it): these thresholds over
        # the ones an earlier accept left for other fields.
        for r in runs:
            old = (conn.execute("select params->'accept' as a from model_run where id = %s", (r,)).fetchone()["a"]
                   or {}).get("thresholds") or {}
            conn.execute("update model_run set params = params || jsonb_build_object('accept', jsonb_build_object("
                         "'decided_by', %s::text, 'at', now(), 'thresholds', %s::jsonb, 'margin', %s::float)) where id = %s",
                         (label, Jsonb({**old, **thresholds}), margin, r))
        if dry_run:
            raise psycopg.Rollback()
    return out


def unaccept(conn: psycopg.Connection, run_id: str | list[str], *, dry_run: bool = False) -> dict:
    """Put every row of the runs a policy accepted back to proposed (the owner's own decisions stay)."""
    runs = [run_id] if isinstance(run_id, str) else list(run_id)
    for r in runs:
        _run(conn, r)
    with conn.transaction():
        rows = conn.execute(
            "with u as (update assignment set status = 'proposed', decided_by = null, decided_at = null"
            " where source_kind = 'model' and source_ref = any(%s) and status = 'active' and decided_by like %s"
            " returning dimension_id) select dimension_id as field, count(*)::int as n from u group by 1 order by 1",
            (runs, POLICY_PREFIX + "%")).fetchall()
        kft = conn.execute(
            "update assignment set status = 'superseded', decided_by = coalesce(evidence->>'superseded_by', 'superseded'),"
            " decided_at = now(), evidence = evidence - 'superseded_by' where source_kind = 'model'"
            " and source_ref = any(%s) and status = 'active' and decided_by like %s", (runs, KIND_FROM_TYPE + "%")).rowcount
        rows = rows + ([{"field": "kind from type", "n": kft}] if kft else [])
        conn.execute("update model_run set params = params - 'accept' where id = any(%s)", (runs,))
        if dry_run:
            raise psycopg.Rollback()
    return {"run_id": ",".join(runs), "runs": runs, "dry_run": dry_run, "fields": {r["field"]: r["n"] for r in rows}}


def format_accept(res: dict) -> str:
    lines = [("DRY RUN (rolled back): " if res["dry_run"] else "")
             + (f"run {res['run_id']}" if len(res["runs"]) == 1 else f"{len(res['runs'])} runs ({res['run_id']})")
             + f", decided_by {res['decided_by']}"]
    for r, n in res.get("boundaries_written", {}).items():
        lines.append(f"  boundary proposals derived for {r}: {n:,} rows")
    for f, d in res["fields"].items():
        lines.append(f"  {f:<7} ≥ {d['threshold']:.2f}: {d['promoted']:>8,} promoted, {d['demoted']:>7,} back to proposed,"
                     f" {d['restamped']:>7,} re-stamped; now {d['active']:,} active, {d['still_proposed']:,} proposed"
                     + (f" ({d['held_back']:,} held back: their side disagrees with the boundary)"
                        if d.get("held_back") else ""))
        if d["promoted_values"]:
            lines.append("               promoted: "
                         + ", ".join(f"{v} {n:,}" for v, n in list(d["promoted_values"].items())[:12]))
        if f == boundary.KIND and res.get("kind_from_type"):
            k = res["kind_from_type"]
            lines.append(f"  kind from type ≥ {k['threshold']:.2f}: {k['brought_back']:,} brought back where the direct kind"
                         f" stayed unsure, {k['returned']:,} returned"
                         + (": " + ", ".join(f"{v} {n:,}" for v, n in list(k["values"].items())[:12]) if k["values"] else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------- the report

def report(conn: psycopg.Connection, run_id: str) -> dict:
    run = _run(conn, run_id)
    prm = run["params"]
    cases_ = conn.execute(
        "select stage, unit_kind, count(*) filter (where error is null)::int as done,"
        " count(*) filter (where error is not null)::int as failed, count(distinct unit_key)::int as units,"
        " coalesce(sum(input_tokens), 0)::bigint as input_tokens, round(avg(record_chars))::int as mean_chars,"
        " round(avg(elapsed_ms))::int as mean_ms"
        " from enrich_case where run_id = %s group by 1, 2 order by 1, 2", (run_id,)).fetchall()
    fields = conn.execute(
        "select p.field, count(*)::int as n, count(*) filter (where p.decided)::int as decided,"
        " count(*) filter (where cardinality(p.selected) > 0)::int as selected,"
        " count(*) filter (where cardinality(p.gate) > 0)::int as gated"
        " from enrich_prediction p join enrich_case c on c.id = p.case_id where c.run_id = %s and not p.fixed"
        " group by 1 order by 1", (run_id,)).fetchall()
    dist: dict[str, dict] = {}
    for r in conn.execute(
            "select p.field, v.value, count(*)::int as n from enrich_prediction p join enrich_case c on c.id = p.case_id"
            " cross join lateral unnest(case when p.field = any(%s) then p.selected else array[p.top] end) v(value)"
            " where c.run_id = %s and not p.fixed and (p.decided or p.field = any(%s))"
            " group by 1, 2 order by 1, 3 desc, 2", (list(MANY), run_id, list(MANY))):
        dist.setdefault(r["field"], {})[r["value"]] = r["n"]
    props = conn.execute(
        "select dimension_id as field, count(distinct entity_id)::int as messages,"
        " count(*) filter (where status = 'proposed')::int as proposed, count(*) filter (where status = 'active')::int"
        " as active, count(*) filter (where status = 'rejected')::int as rejected,"
        " count(*) filter (where (evidence ->> 'propagated')::boolean)::int as propagated"
        " from assignment where source_kind = 'model' and source_ref = %s group by 1 order by 1", (run_id,)).fetchall()
    tpl = conn.execute(
        """
        with t as (select c.unit_key, p.field, count(*) as k,
                          count(distinct case when p.field = any(%s)
                                              then array_to_string(array(select x from unnest(p.selected) x order by x), ',')
                                              else coalesce(p.top, '') end) as sigs
                   from enrich_case c join enrich_prediction p on p.case_id = c.id
                   where c.run_id = %s and c.unit_kind = 'template' and c.error is null and not p.fixed
                   group by 1, 2)
        select field, count(*) filter (where k = 3 and sigs = 1)::int as agreed,
               count(*) filter (where k = 3 and sigs > 1)::int as split,
               count(*) filter (where k < 3)::int as incomplete
        from t group by 1 order by 1""", (list(MANY), run_id)).fetchall()
    return {"run_id": run_id, "stage": prm.get("stage"), "model": run["model"],
            "question_version": prm.get("question_version"), "policy": prm.get("policy"),
            "cases": cases_, "fields": fields, "distribution": dist, "proposals": props,
            "templates": tpl,
            "cost": {k: prm.get(k) for k in ("cases_done", "failed", "requests", "retries", "input_tokens",
                                              "output_tokens", "cost_usd", "wall_seconds", "invocations")}}


def _pct(a: int, b: int) -> str:
    return f"{100 * a / b:.1f}%" if b else "—"


def format_report(rep: dict) -> str:
    c = rep["cost"]
    lines = [f"Backfill run {rep['run_id']}: stage {rep['stage']}, {rep['model']}, question version"
             f" {rep['question_version']}",
             f"  {c['cases_done'] or 0:,} cases done, {c['failed'] or 0:,} failed; {c['requests'] or 0:,} requests"
             f" ({c['retries'] or 0:,} retries); {c['input_tokens'] or 0:,} input tokens"
             f" ({(c['input_tokens'] or 0) // max(1, c['cases_done'] or 0):,} per case), ${c['cost_usd'] or 0:.2f};"
             f" {c['wall_seconds'] or 0:.0f} s over {c['invocations'] or 0} invocation(s)"
             + (f" ({(c['cases_done'] or 0) / c['wall_seconds']:.1f} cases/s)" if c.get("wall_seconds") else ""),
             "", "Cases per unit kind:"]
    for r in rep["cases"]:
        lines.append(f"  {r['stage']:<8} {r['unit_kind']:<9} {r['units']:>7,} units {r['done']:>7,} done {r['failed']:>5,}"
                     f" failed, mean record {r['mean_chars'] or 0:,} chars, mean {r['mean_ms'] or 0:,} ms")
    lines += ["", "Decided per field (one-value: top ≥ p and margin ≥ m; ask and route: a value selected after the gates):"]
    for r in rep["fields"]:
        many = r["field"] in MANY
        lines.append(f"  {r['field']:<7} {r['n']:>7,} cases, "
                     + (f"{r['selected']:,} with a value ({_pct(r['selected'], r['n'])}), {r['gated']:,} gated to none"
                        if many else f"{r['decided']:,} decided ({_pct(r['decided'], r['n'])})"))
    lines += ["", "Values per field (decided cases; ask and route: the selected values):"]
    for f, d in rep["distribution"].items():
        lines.append(f"  {f:<7} " + ", ".join(f"{v} {n:,}" for v, n in list(d.items())[:15])
                     + (f", … {len(d) - 15} more" if len(d) > 15 else ""))
    lines += ["", "Proposals on messages (assignment rows of the run):"]
    for r in rep["proposals"]:
        lines.append(f"  {r['field']:<7} {r['messages']:>8,} messages; {r['proposed']:,} proposed, {r['active']:,} active,"
                     f" {r['rejected']:,} rejected; {r['propagated']:,} propagated")
    if rep["templates"]:
        lines += ["", "Templates (3 samples): agreed (propagated to the template) / split (samples only) / incomplete:"]
        for r in rep["templates"]:
            lines.append(f"  {r['field']:<7} {r['agreed']:>6,} / {r['split']:>6,} / {r['incomplete']:>4,}"
                         f"  ({_pct(r['agreed'], r['agreed'] + r['split'])} agreed)")
    return "\n".join(lines)
