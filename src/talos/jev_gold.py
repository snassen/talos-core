"""Enrichment step C: Jev on the answer key, and how well it did (docs/enrichment-plan.md §7, §11.3).

Evaluation only: nothing here writes an assignment. A run sends one case per answer-key item,
in one unit design (talos.jev: `message` or `context`), stores what Jev answered and how the
policy (talos.policy) routed it, and scores it against the answer key.

**A run** is a `model_run` row (purpose `gold-eval`) whose params hold the set, the unit, the
question version, the record and masking versions, the policy, and the running totals: cases,
requests, retries, input and output tokens, cost at $0.042 per million input tokens, and wall
time. Each answered case is a `jev_case` row (the record's hash and size, usage, time) with its
six `jev_prediction` rows, written together, so a run stopped halfway resumes where it was:
`run(..., run_id=the same)` skips the items it has. A resumed run must ask the same questions
of the same records, or it is refused.

**Guards.** A run refuses to send more than max_cases (400) cases at once. A dry run builds
every request, prints one of them whole (exactly as it would be sent, key aside), estimates
tokens and cost for all, and sends nothing, reads no key and writes nothing.

**The report** scores a run against the combined reference (gold.combined_labels: the owner's
label where they gave one, otherwise Claude's). A field Claude (or the owner) marked "not sure" is left out of
accuracy and counted apart, as gold.compare() does. Per field: accuracy on the decided cases
and coverage (the share decided); top-1 accuracy over all cases with accuracy per confidence
bucket (gold.bucket); a threshold sweep (coverage against accuracy, re-routed from the stored
probabilities, no new call); the most common mistakes; and cost, tokens and time. Email and
Teams are also scored apart: a Teams item is its whole window in both unit designs, so only
the email split can decide the unit. With a second run, both are scored on the items both
answered, side by side, per split.

**Question templates** (talos.jev.TEMPLATE_VERSION). A run records the templates it asked in
params.template_version (v1 runs have 1; a run without it is read from its scores: a `none`
among the ask or route scores means v2). The report re-routes each run by its own kind
(talos.policy.route_case): v1 ask and route as statements, swept over the statement threshold
s; v2 ask and route as one choice plus the cross-field gates, swept over the one-value
thresholds p and m (the deadline statement stays at s = 0.5). For a v2 run the report also
counts the gates: how often each forced a field to none, and how often the reference agrees
that it is none. v2 Teams windows are asked their own set (origin fixed to person).
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

from talos import gold, jev, mask, policy
from talos.gold import FIELDS, MANY_FIELDS, Prediction
from talos.policy import Policy

PURPOSE = "gold-eval"
MAX_CASES = 400
PROGRESS_EVERY = 10
# The sweep's grid: one-value fields (top probability, margin), many-value fields (score).
SWEEP_ONE = ((0.0, 0.0), (0.5, 0.0), (0.5, 0.15), (0.6, 0.15), (0.7, 0.0), (0.7, 0.15), (0.7, 0.3),
             (0.8, 0.15), (0.85, 0.3), (0.9, 0.3), (0.95, 0.4))
SWEEP_MANY = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
# The report also scores email and Teams apart: Teams items are the same window in both unit
# designs (talos.jev.record), so only the email split can decide the unit.
SPLITS = ("email", "teams")


class JevRunError(ValueError):
    pass


# ---------------------------------------------------------------- the plan

def _set(conn: psycopg.Connection, set_id: int) -> dict:
    s = conn.execute("select id, name from gold_set where id = %s", (set_id,)).fetchone()
    if not s:
        raise JevRunError(f"no answer key {set_id}")
    return s


def plan(conn: psycopg.Connection, set_id: int, unit: str, *, limit: int | None = None,
         skip: set[int] | None = None) -> list[dict]:
    """The cases of a run, in answer-key order: [{item_id, position, record}], leaving out the
    items in skip. limit takes the first N items of the set (before skipping)."""
    _set(conn, set_id)
    items = conn.execute("select id, position from gold_item where set_id = %s order by position",
                         (set_id,)).fetchall()
    if limit is not None:
        items = items[:max(0, limit)]
    out = []
    for i in items:
        if skip and i["id"] in skip:
            continue
        it = gold.item(conn, set_id, i["position"])
        out.append({"item_id": i["id"], "position": i["position"], "record": jev.record(it, unit)})
    return out


def _params(set_id: int, unit: str, qversion: str, pol: Policy, client: jev.JevClient | None, limit,
            template: int = jev.TEMPLATE_VERSION) -> dict:
    return {"set_id": set_id, "unit": unit, "question_version": qversion,
            "template_version": template, "record_version": jev.RECORD_VERSION,
            "mask_version": mask.VERSION, "policy": pol.as_dict(), "limit": limit,
            "endpoint": client.endpoint if client else jev.ENDPOINT,
            "concurrency": client.concurrency if client else jev.CONCURRENCY,
            "price_per_mtok_input": jev.PRICE_PER_MTOK_INPUT, "body_max": jev.BODY_MAX,
            "record_max": jev.RECORD_MAX,
            "cases_done": 0, "failed": 0, "requests": 0, "retries": 0, "input_tokens": 0, "output_tokens": 0,
            "cost_usd": 0.0, "wall_seconds": 0.0, "invocations": 0}


SAME = ("set_id", "unit", "question_version", "template_version", "record_version", "mask_version")


# ---------------------------------------------------------------- the run

def run(conn: psycopg.Connection, set_id: int, unit: str, *, client: jev.JevClient | None = None,
        run_id: str | None = None, limit: int | None = None, max_cases: int = MAX_CASES,
        dry_run: bool = False, pol: Policy = policy.DEFAULT, template: int = jev.TEMPLATE_VERSION,
        progress: Callable[[str], None] = print) -> dict:
    """Run Jev on an answer key (or resume run_id), or with dry_run show what would be sent.
    Returns a summary. See the module docstring."""
    if unit not in jev.UNITS:
        raise JevRunError(f"unit is one of {', '.join(jev.UNITS)}")
    _set(conn, set_id)
    dims = jev.taxonomy_from_db(conn)
    if template not in jev.TEMPLATES:
        raise JevRunError(f"question templates are one of {', '.join(map(str, jev.TEMPLATES))}")
    if template == 1:
        questions = jev.build_questions(dims, template=1)
        qsets = {m: questions for m in jev.MEDIA}
        qversion = jev.question_version(questions, template=1)
    else:
        qsets = jev.question_sets(dims, template)
        qversion = jev.question_version(qsets, template=template)
    model = client.model if client else jev.MODEL
    done: set[int] = set()
    existing = None
    if run_id:
        existing = conn.execute("select * from model_run where id = %s", (run_id,)).fetchone()
        if existing:
            if existing["purpose"] != PURPOSE:
                raise JevRunError(f"run {run_id} is not a Jev answer-key run")
            want = _params(set_id, unit, qversion, pol, client, limit, template)
            diff = [k for k in SAME if existing["params"].get(k) != want[k]]
            if diff:
                raise JevRunError(f"run {run_id} was made with another {', '.join(diff)}; start a new run instead")
            done = {r["item_id"] for r in conn.execute("select item_id from jev_case where run_id = %s", (run_id,))}
    cases = plan(conn, set_id, unit, limit=limit, skip=done)
    bodies = [(c["item_id"], jev.request_body(c["record"], jev.questions_for(qsets, c["record"]), model=model))
              for c in cases]
    sizes = [jev.record_chars(c["record"]) for c in cases]
    est = sum(jev.estimate_tokens(b) for _, b in bodies)
    summary = {"set_id": set_id, "unit": unit, "model": model, "question_version": qversion,
               "template_version": template, "questions": {m: len(q) for m, q in qsets.items()},
               "already_done": len(done), "to_send": len(cases),
               "estimated_input_tokens": est, "estimated_cost_usd": round(jev.cost(est), 4),
               "record_chars": _spread(sizes)}
    if dry_run:
        summary["dry_run"] = True
        summary["request"] = bodies[0][1] if bodies else None
        summary["position"] = cases[0]["position"] if cases else None
        summary["over_max_cases"] = len(cases) > max_cases
        return summary
    if len(cases) > max_cases:
        raise JevRunError(f"refused: {len(cases)} cases to send is over --max-cases {max_cases}"
                          f" (estimated {est:,} input tokens, ${jev.cost(est):.4f}); raise it or use --limit")
    if client is None:
        client = jev.JevClient()
    if not existing:
        run_id = run_id or f"jev-gold{set_id}-{unit}-{qversion[:8]}-{jev.now_stamp()}"
        params = _params(set_id, unit, qversion, pol, client, limit, template)
        planned = len(plan_ids(conn, set_id, limit))
        conn.execute("insert into model_run (id, model, purpose, params, input_count) values (%s, %s, %s, %s, %s)",
                     (run_id, model, PURPOSE, Jsonb(params), planned))
    summary["run_id"] = run_id
    by_id = {c["item_id"]: c for c in cases}
    state = {"done": 0, "failed": [], "t0": time.monotonic()}
    total = len(cases)
    progress(f"jev: run {run_id}: {total} cases to send ({len(done)} already done), {client.concurrency} in flight,"
             f" about {est:,} input tokens (${jev.cost(est):.4f})")

    def on_result(res: jev.Result) -> None:
        c = by_id[res.case_id]
        if res.error is None:
            try:
                _store(conn, run_id, c, res, dims, pol, model, template)
            except jev.JevError as exc:
                res.error = str(exc)
        if res.error is not None:
            state["failed"].append((c["position"], res.error))
        else:
            state["done"] += 1
        n = state["done"] + len(state["failed"])
        if n % PROGRESS_EVERY == 0 or n == total:
            progress(f"jev: {n}/{total} ({state['done']} stored, {len(state['failed'])} failed),"
                     f" {time.monotonic() - state['t0']:.1f} s, {client.usage.input_tokens:,} input tokens")

    before = client.usage.as_dict()
    error = None
    try:
        asyncio.run(client.run(bodies, on_result))
    except jev.JevAuthError as exc:
        error = str(exc)
    finally:
        _account(conn, run_id, client, before, time.monotonic() - state["t0"], len(state["failed"]))
    summary.update(stored=state["done"], failed=[{"position": p, "error": e} for p, e in state["failed"]],
                   usage={k: client.usage.as_dict()[k] - before[k] for k in before},
                   seconds=round(time.monotonic() - state["t0"], 2))
    summary["cost_usd"] = round(jev.cost(summary["usage"]["input_tokens"]), 6)
    if error:
        summary["error"] = error
    return summary


def plan_ids(conn: psycopg.Connection, set_id: int, limit: int | None) -> list[int]:
    ids = [r["id"] for r in conn.execute("select id from gold_item where set_id = %s order by position", (set_id,))]
    return ids if limit is None else ids[:max(0, limit)]


def _spread(xs: list[int]) -> dict:
    if not xs:
        return {}
    return {"min": min(xs), "median": int(statistics.median(xs)), "mean": round(statistics.mean(xs)),
            "max": max(xs)}


def _store(conn: psycopg.Connection, run_id: str, case: dict, res: jev.Result, dims: dict, pol: Policy,
           model: str, template: int = jev.TEMPLATE_VERSION) -> None:
    """One answered case: its jev_case row and its six predictions, in one transaction. Routed
    (and for v2 gated) by policy.route_case; a field a gate changed keeps in raw["gate"] the
    gates that fired and in raw["ungated"] what it was before."""
    rec = case["record"]
    fields = jev.read_answers(dims, res.response.get("answers"), template=template, teams=rec.get("unit") == "window")
    decisions, fired = policy.route_case({f: a["scores"] for f, a in fields.items()}, pol, template=template)
    with conn.transaction():
        conn.execute(
            "insert into jev_case (run_id, item_id, record_sha256, record_chars, model, input_tokens, output_tokens,"
            " elapsed_ms) values (%s, %s, %s, %s, %s, %s, %s, %s)",
            (run_id, case["item_id"], jev.record_sha(rec), jev.record_chars(rec), res.response.get("model") or model,
             res.usage.get("input_tokens", 0), res.usage.get("output_tokens", 0), int(res.seconds * 1000)))
        for f, a in fields.items():
            d = decisions[f]
            raw = a["raw"]
            if f in fired:
                before = policy.route_choice_many(f, a["scores"], pol, policy.CHOICE_MANY[f])
                raw = {**raw, "gate": fired[f], "ungated": list(before.selected)}
            conn.execute(
                "insert into jev_prediction (run_id, item_id, field, top, selected, scores, confidence, margin,"
                " decided, raw) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (run_id, case["item_id"], f, d.top, list(d.selected), Jsonb(a["scores"]), d.confidence, d.margin,
                 d.decided, Jsonb(raw)))


def _account(conn: psycopg.Connection, run_id: str, client: jev.JevClient, before: dict, seconds: float,
             failed: int) -> None:
    """Add this invocation's usage, cost and time to the run's totals."""
    row = conn.execute("select params from model_run where id = %s", (run_id,)).fetchone()
    p = dict(row["params"])
    now = client.usage.as_dict()
    for k in ("requests", "retries", "input_tokens", "output_tokens"):
        p[k] = int(p.get(k) or 0) + now[k] - before[k]
    p["cost_usd"] = round(jev.cost(p["input_tokens"], p.get("price_per_mtok_input", jev.PRICE_PER_MTOK_INPUT)), 6)
    p["wall_seconds"] = round(float(p.get("wall_seconds") or 0) + seconds, 3)
    p["invocations"] = int(p.get("invocations") or 0) + 1
    p["failed"] = failed
    n = conn.execute("select count(*)::int as n from jev_case where run_id = %s", (run_id,)).fetchone()["n"]
    p["cases_done"] = n
    conn.execute("update model_run set params = %s, output_count = %s where id = %s", (Jsonb(p), n, run_id))


def format_dry_run(s: dict) -> str:
    lines = ["DRY RUN: nothing is sent, no key is read, nothing is stored.",
             f"Answer key {s['set_id']}, unit {s['unit']}, model {s['model']}, question templates"
             f" v{s['template_version']}, question version {s['question_version']}"
             f" ({', '.join(f'{m} {n}' for m, n in s['questions'].items())} questions)",
             f"{s['to_send']} cases would be sent" + (f" ({s['already_done']} already done)" if s["already_done"] else ""),
             f"record size in characters: {s['record_chars']}",
             f"estimated input tokens for the run: {s['estimated_input_tokens']:,}"
             f" (about {s['estimated_input_tokens'] // max(1, s['to_send']):,} per case),"
             f" cost about ${s['estimated_cost_usd']:.4f} at ${jev.PRICE_PER_MTOK_INPUT} per million input tokens"
             " (output is free); the estimate is ±15%",
             ]
    if s.get("over_max_cases"):
        lines.append("a real run would be REFUSED: over --max-cases")
    if s.get("request") is not None:
        lines += ["", f"The request for item {s['position']}, exactly as it would be sent:",
                  f"POST {jev.ENDPOINT}",
                  f"Authorization: Bearer <the Keychain item {jev.KEY_NAME!r}; not read in a dry run>",
                  "Content-Type: application/json", "",
                  json.dumps(s["request"], ensure_ascii=False, indent=2)]
    return "\n".join(lines)


# ---------------------------------------------------------------- predictions and the report

def _run(conn: psycopg.Connection, run_id: str) -> dict:
    r = conn.execute("select * from model_run where id = %s", (run_id,)).fetchone()
    if not r or r["purpose"] != PURPOSE:
        raise JevRunError(f"no Jev answer-key run {run_id!r}")
    return r


def stored(conn: psycopg.Connection, run_id: str) -> dict[int, dict[str, dict]]:
    """A run's stored answers: {item_id: {field: row}}."""
    out: dict[int, dict] = {}
    for r in conn.execute("select item_id, field, top, selected, scores, confidence, margin, decided"
                          " from jev_prediction where run_id = %s", (run_id,)):
        out.setdefault(r["item_id"], {})[r["field"]] = r
    return out


def template_of(run: dict, rows: dict[int, dict[str, dict]]) -> int:
    """The question templates a run asked: params.template_version, else read from what is
    stored (a `none` among the ask or route scores is a v2 choice)."""
    t = (run.get("params") or {}).get("template_version")
    if t is not None:
        return int(t)
    return 2 if any(policy.NONE in (fs[f]["scores"] or {}) for fs in rows.values() for f in MANY_FIELDS
                    if f in fs) else 1


def _routed(fields: dict[str, dict], pol: Policy, template: int):
    return policy.route_case({f: r["scores"] for f, r in fields.items()}, pol, template=template)


def predictions(rows: dict[int, dict[str, dict]], *, source: str, pol: Policy | None = None,
                decided_only: bool = True, items: set[int] | None = None,
                template: int = 1) -> dict[int, dict[str, Prediction]]:
    """Stored answers as predictions for gold.compare(). With pol, re-routed from the stored
    scores under that policy, the way the run's question templates need (v1: ask and route as
    statements; v2: one choice each, then the gates); otherwise as the run routed them.
    decided_only leaves an undecided one-value field out (it counts against coverage);
    otherwise its top value is the prediction. Ask and route are always predicted (an empty
    selection is none)."""
    out: dict[int, dict] = {}
    for item_id, fields in rows.items():
        if items is not None and item_id not in items:
            continue
        routed = _routed(fields, pol, template)[0] if pol is not None else None
        for f, r in fields.items():
            many = f in MANY_FIELDS
            if routed is not None:
                d = routed[f]
                top, selected, conf, decided = d.top, d.selected, d.confidence, d.decided
            else:
                top, selected, conf, decided = r["top"], tuple(r["selected"]), r["confidence"], r["decided"]
            if many:
                out.setdefault(item_id, {})[f] = Prediction(tuple(selected), conf, source)
            elif decided:
                out.setdefault(item_id, {})[f] = Prediction((top,), conf, source)
            elif not decided_only and top is not None:
                out.setdefault(item_id, {})[f] = Prediction((top,), conf, source)
    return out


def _many_micro(res: dict) -> tuple[float | None, float | None]:
    tp = sum(d["tp"] for d in res["per_value"].values())
    fp = sum(d["fp"] for d in res["per_value"].values())
    fn = sum(d["fn"] for d in res["per_value"].values())
    return gold._rate(tp, tp + fp), gold._rate(tp, tp + fn)


def _many_point(r: dict) -> dict:
    precision, recall = _many_micro(r)
    return {"exact": r["accuracy"], "precision": precision, "recall": recall,
            "any_precision": r["any"]["precision"], "any_recall": r["any"]["recall"]}


def sweep(ref: dict, rows: dict, items: set[int], source: str, template: int = 1) -> dict[str, list[dict]]:
    """Coverage against accuracy at several thresholds, re-routed from the stored scores. v1
    sweeps ask and route over the statement threshold s; v2 over the one-value thresholds p
    and m (its ask and route are one choice each; the deadline statement stays at 0.5)."""
    out: dict[str, list[dict]] = {f: [] for f in FIELDS}
    for p, m in SWEEP_ONE:
        pol = Policy(one_min=p, one_margin=m)
        fields = FIELDS if template >= 2 else [f for f in FIELDS if f not in MANY_FIELDS]
        sc = gold.compare(ref, predictions(rows, source=source, pol=pol, items=items, template=template),
                          fields=fields)
        for f, r in sc.items():
            if f in MANY_FIELDS:
                out[f].append({"min": p, "margin": m, **_many_point(r)})
            else:
                out[f].append({"min": p, "margin": m, "coverage": r["coverage"], "accuracy": r["accuracy"],
                               "decided": r["predicted"], "correct": r["correct"]})
    if template >= 2:
        return out
    for s in SWEEP_MANY:
        pol = Policy(many_min=s)
        sc = gold.compare(ref, predictions(rows, source=source, pol=pol, items=items, template=template),
                          fields=list(MANY_FIELDS))
        for f, r in sc.items():
            out[f].append({"min": s, **_many_point(r)})
    return out


def gate_counts(ref: dict, rows: dict, items: set[int], pol: Policy, template: int) -> dict[str, dict]:
    """How much each gate did (v2; {} for v1), re-routed under pol: per gate how many items it
    forced to none (fired), and of those how many the reference labels none for sure (right),
    with a value for sure (wrong), or not for sure (unscored)."""
    if template < 2:
        return {}
    out = {g: {"fired": 0, "right": 0, "wrong": 0, "unscored": 0} for g in policy.GATES}
    for item_id, fields in rows.items():
        if item_id not in items:
            continue
        _, fired = _routed(fields, pol, template)
        for f, gates in fired.items():
            g = (ref.get(item_id) or {}).get(f)
            verdict = "unscored" if not g or g["status"] != "set" else "wrong" if g["values"] else "right"
            for name in gates:
                out[name]["fired"] += 1
                out[name][verdict] += 1
    return out


def _cost(run: dict, items: int) -> dict:
    p = run["params"]
    return {"cases": p.get("cases_done", 0), "planned": run["input_count"], "requests": p.get("requests", 0),
            "retries": p.get("retries", 0), "failed": p.get("failed", 0),
            "input_tokens": p.get("input_tokens", 0), "output_tokens": p.get("output_tokens", 0),
            "input_tokens_per_case": round(p.get("input_tokens", 0) / max(1, p.get("cases_done", 0))),
            "cost_usd": p.get("cost_usd", 0.0), "wall_seconds": p.get("wall_seconds", 0.0),
            "cases_per_second": round(p.get("cases_done", 0) / p["wall_seconds"], 1) if p.get("wall_seconds") else None,
            "scored_items": items}


def _score(ref: dict, rows: dict, items: set[int], source: str, template: int = 1) -> dict:
    decided = gold.compare(ref, predictions(rows, source=source, items=items, template=template))
    top = gold.compare(ref, predictions(rows, source=source, decided_only=False, items=items, template=template))
    out = {}
    for f in FIELDS:
        d, t = decided[f], top[f]
        r = {"labelled": d["labelled"], "unsure": d["unsure"], "skipped": d["skipped"]}
        if f in MANY_FIELDS:
            precision, recall = _many_micro(d)
            r.update(exact=d["accuracy"], precision=precision, recall=recall, any=d["any"],
                     any_precision=d["any"]["precision"], any_recall=d["any"]["recall"],
                     per_value=d["per_value"], by_bucket=d["by_bucket"],
                     mistakes=dict(Counter(d["by_source"].get(source, {}).get("wrong", {})).most_common(8)))
        else:
            r.update(decided=d["predicted"], coverage=d["coverage"], accuracy=d["accuracy"], correct=d["correct"],
                     top1=t["accuracy"], top1_correct=t["correct"], top1_n=t["predicted"], by_bucket=t["by_bucket"],
                     mistakes=dict(Counter(d["by_source"].get(source, {}).get("wrong", {})).most_common(8)),
                     mistakes_all=dict(Counter(t["by_source"].get(source, {}).get("wrong", {})).most_common(8)))
        out[f] = r
    return out


def report(conn: psycopg.Connection, run_id: str, *, compare_run: str | None = None) -> dict:
    """The evaluation of a run (and side by side with compare_run). See the module docstring."""
    run_a = _run(conn, run_id)
    set_id = run_a["params"]["set_id"]
    runs = [run_a]
    if compare_run:
        run_b = _run(conn, compare_run)
        if run_b["params"]["set_id"] != set_id:
            raise JevRunError(f"runs {run_id} and {compare_run} are on different answer keys")
        runs.append(run_b)
    ref, sources = gold.combined_labels(conn, set_id)
    rows = {r["id"]: stored(conn, r["id"]) for r in runs}
    items = set.intersection(*(set(v) for v in rows.values())) if rows else set()
    ref = {i: v for i, v in ref.items() if i in items}
    positions, medium = {}, {}
    for r in conn.execute("select i.id, i.position, m.medium from gold_item i join message m on m.id = i.message_id"
                          " where i.set_id = %s", (set_id,)):
        positions[r["id"]] = r["position"]
        medium[r["id"]] = "email" if r["medium"] == "email" else "teams"
    splits = {"all": items, **{k: {i for i in items if medium.get(i) == k} for k in SPLITS}}
    out = {"set_id": set_id, "items": len(items), "split_items": {k: len(v) for k, v in splits.items()},
           "reference": {
               f: {"from": dict(sources[f]),
                   "sure": sum(1 for v in ref.values() if f in v and v[f]["status"] == "set"),
                   "unsure": sum(1 for v in ref.values() if f in v and v[f]["status"] == "unsure"),
                   "skipped": sum(1 for v in ref.values() if f in v and v[f]["status"] == "skip")} for f in FIELDS},
           "runs": []}
    for r in runs:
        p = r["params"]
        rs = rows[r["id"]]
        t = template_of(r, rs)
        pol = _policy(p.get("policy") or {})
        by = {}
        for k, ids in splits.items():
            sub = {i: v for i, v in ref.items() if i in ids}
            by[k] = {"items": len(ids), "scores": _score(sub, rs, ids, r["id"], t),
                     "sweep": sweep(sub, rs, ids, r["id"], t), "gates": gate_counts(sub, rs, ids, pol, t)}
        out["runs"].append({
            "id": r["id"], "model": r["model"], "unit": p["unit"], "question_version": p["question_version"],
            "template_version": t, "policy": p["policy"], "cost": _cost(r, len(items)),
            "scores": by["all"]["scores"], "sweep": by["all"]["sweep"], "gates": by["all"]["gates"],
            "splits": {k: by[k] for k in SPLITS},
            "undecided_items": {f: sorted(positions.get(i, i) for i, fs in rs.items()
                                          if i in items and f in fs and not fs[f]["decided"])
                                for f in FIELDS if f not in MANY_FIELDS}})
    return out


def _policy(d: dict) -> Policy:
    """The policy a run stored (its own version kept)."""
    return Policy(**{k: d[k] for k in ("one_min", "one_margin", "many_min", "version") if k in d})


def _cells(f: str, s: dict) -> str:
    if f in MANY_FIELDS:
        return f"{gold.pct(s['exact'])} / {gold.pct(s['precision'])} / {gold.pct(s['recall'])}"
    return f"{gold.pct(s['coverage'])} / {gold.pct(s['accuracy'])} / {gold.pct(s['top1'])}"


def _format_sweep(sw: dict, template: int = 1) -> list[str]:
    one = [f for f in FIELDS if f not in MANY_FIELDS]
    lines = ["  Threshold sweep, one-value fields (decided when top ≥ p and margin ≥ m): coverage / accuracy",
             "    " + f"{'p':>4} {'m':>5}  " + "".join(f"{f:<17}" for f in one)]
    for k, pt in enumerate(sw[one[0]]):
        lines.append("    " + f"{pt['min']:>4.2f} {pt['margin']:>5.2f}  " + "".join(
            f"{gold.pct(sw[f][k]['coverage']) + ' / ' + gold.pct(sw[f][k]['accuracy']):<17}" for f in one))
    cell = lambda f, k: f"{' / '.join(gold.pct(sw[f][k][x]) for x in ('exact', 'precision', 'recall')):<26}"  # noqa: E731
    if template >= 2:
        lines += ["  Threshold sweep, ask and route (one choice: selected when its top is not none, top ≥ p and"
                  " margin ≥ m; gated): exact set / precision / recall",
                  "    " + f"{'p':>4} {'m':>5}  " + "".join(f"{f:<26}" for f in MANY_FIELDS)]
        for k, pt in enumerate(sw[MANY_FIELDS[0]]):
            lines.append("    " + f"{pt['min']:>4.2f} {pt['margin']:>5.2f}  " + "".join(cell(f, k) for f in MANY_FIELDS))
        return lines
    lines += ["  Threshold sweep, ask and route (selected when score ≥ s): exact set / precision / recall",
              "    " + f"{'s':>4}  " + "".join(f"{f:<26}" for f in MANY_FIELDS)]
    for k, pt in enumerate(sw[MANY_FIELDS[0]]):
        lines.append("    " + f"{pt['min']:>4.2f}  " + "".join(cell(f, k) for f in MANY_FIELDS))
    return lines


def _format_gates(g: dict) -> str:
    return ", ".join(f"{name} {d['fired']} (right {d['right']}, wrong {d['wrong']}, unscored {d['unscored']})"
                     for name, d in g.items())


def format_report(rep: dict) -> str:
    runs = rep["runs"]
    names = [f"{r['unit']} ({r['id']})" for r in runs]
    cols = [f"{r['unit']} v{r['template_version']}" for r in runs]
    if len(cols) > 1 and cols[0] == cols[1]:
        cols = [f"{c} {x}" for c, x in zip(cols, "AB")]
    lines = [f"Jev on answer key {rep['set_id']}: {rep['items']} items scored"
             + (" (the items both runs answered)" if len(runs) > 1 else ""),
             "Reference: your label where you gave one, otherwise Claude's; not sure is left out of accuracy.",
             ""]
    lines.append("Reference per field (sure / not sure / skipped; from whom):")
    for f, r in rep["reference"].items():
        lines.append(f"  {f:<7} {r['sure']:>4} / {r['unsure']:>3} / {r['skipped']:>3}   "
                     + ", ".join(f"{k} {v}" for k, v in sorted(r["from"].items())))
    for r in runs:
        c = r["cost"]
        kind = "ask and route as one choice, gated" if r["template_version"] >= 2 else "ask and route as statements"
        lines += ["", f"Run {r['id']}: unit {r['unit']}, {r['model']}, question templates v{r['template_version']}"
                      f" ({kind}),"
                      f" questions {r['question_version']}, policy {_policy(r['policy']).name}",
                  f"  {c['cases']} of {c['planned']} cases, {c['requests']} requests ({c['retries']} retries,"
                  f" {c['failed']} failed), {c['input_tokens']:,} input tokens ({c['input_tokens_per_case']:,} per case),"
                  f" {c['output_tokens']:,} output tokens, ${c['cost_usd']:.4f}, {c['wall_seconds']:.1f} s"
                  + (f" ({c['cases_per_second']} cases/s)" if c["cases_per_second"] else "")]
        for f, s in r["scores"].items():
            head = f"  {f}: {s['labelled']} labelled sure ({s['unsure']} not sure, {s['skipped']} skipped, left out)"
            if f in MANY_FIELDS:
                lines.append(head)
                lines.append(f"    exact set {gold.pct(s['exact'])}; per value precision {gold.pct(s['precision'])},"
                             f" recall {gold.pct(s['recall'])}; any: precision {gold.pct(s['any']['precision'])},"
                             f" recall {gold.pct(s['any']['recall'])}")
                vals = [f"{v} P {gold.pct(d['precision'])} R {gold.pct(d['recall'])}" for v, d in s["per_value"].items()]
                if vals:
                    lines.append("    per value: " + ", ".join(vals))
            else:
                lines.append(head)
                lines.append(f"    decided {s['decided']} ({gold.pct(s['coverage'])} coverage), accuracy on decided"
                             f" {s['correct']}/{s['decided']} ({gold.pct(s['accuracy'])}); top-1 over all"
                             f" {s['top1_correct']}/{s['top1_n']} ({gold.pct(s['top1'])})")
                if s["by_bucket"]:
                    lines.append("    top-1 by confidence: " + ", ".join(
                        f"{b} {d['correct']}/{d['n']} ({gold.pct(d['precision'])})" for b, d in s["by_bucket"].items()))
            if s["mistakes"]:
                lines.append("    top mistakes (Jev→reference): " + ", ".join(f"{k} {n}" for k, n in s["mistakes"].items()))
        lines.append("  By medium (one-value: coverage / accuracy on decided / top-1; ask and route: exact / precision"
                     " / recall), all | email | Teams (" + " | ".join(
                         str(rep["split_items"][k]) for k in ("all", *SPLITS)) + " items):")
        for f in FIELDS:
            cells = [_cells(f, r["scores"][f])] + [_cells(f, r["splits"][k]["scores"][f]) for k in SPLITS]
            lines.append(f"    {f:<7} " + "  |  ".join(cells))
        if r["gates"]:
            lines.append("  Gates (items forced to none; right/wrong by the reference, unscored when not labelled"
                         " sure):")
            for k in ("all", *SPLITS):
                g = r["gates"] if k == "all" else r["splits"][k]["gates"]
                lines.append(f"    {k:<6} {_format_gates(g)}")
        lines.extend(_format_sweep(r["sweep"], r["template_version"]))
    if len(runs) > 1:
        a, b = runs
        lines += ["", f"Side by side: {names[0]}  vs  {names[1]}",
                  "  (email decides the unit: a Teams item is the same window in both designs)"]
        for split in ("email", "teams", "all"):
            n = rep["split_items"][split]
            sa = a["scores"] if split == "all" else a["splits"][split]["scores"]
            sb = b["scores"] if split == "all" else b["splits"][split]["scores"]
            lines.append(f"  {split} ({n} items)")
            lines.append(f"    {'field':<7} {'measure':<22} {cols[0]:>12} {cols[1]:>12}")
            for f in FIELDS:
                if f in MANY_FIELDS:
                    measures = (("exact set", "exact"), ("precision", "precision"), ("recall", "recall"),
                                ("any: precision", "any_precision"), ("any: recall", "any_recall"))
                else:
                    measures = (("coverage", "coverage"), ("accuracy on decided", "accuracy"), ("top-1 over all", "top1"))
                for label, k in measures:
                    lines.append(f"    {f:<7} {label:<22} {gold.pct(sa[f][k]):>12} {gold.pct(sb[f][k]):>12}")
            ga = a["gates"] if split == "all" else a["splits"][split]["gates"]
            gb = b["gates"] if split == "all" else b["splits"][split]["gates"]
            for name in policy.GATES:
                if name in ga or name in gb:
                    ca_, cb_ = (f"{g[name]['fired']} ({g[name]['wrong']} wrong)" if name in g else "—" for g in (ga, gb))
                    lines.append(f"    {'gate':<7} {name:<22} {ca_:>12} {cb_:>12}")
        ca, cb = a["cost"], b["cost"]
        lines.append(f"  {'cost':<7} {'input tokens per case':<22} {ca['input_tokens_per_case']:>12,}"
                     f" {cb['input_tokens_per_case']:>12,}")
        lines.append(f"  {'cost':<7} {'dollars':<22} {ca['cost_usd']:>12.4f} {cb['cost_usd']:>12.4f}")
        lines.append(f"  {'time':<7} {'wall seconds':<22} {ca['wall_seconds']:>12.1f} {cb['wall_seconds']:>12.1f}")
    return "\n".join(lines)
