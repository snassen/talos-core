"""The acceptance explorer (Operations › Acceptance): what a confidence level gives, per field.

docs/enrichment-plan.md §11. For each of the nine fields of two-level acceptance (the four
boundaries, kind, and origin, type, topic and value) the owner moves a threshold and sees, at once:

- on the **answer key**: the share Jev decides and the share of those that is right, measured
  against gold.combined_labels (the owner's label, else Claude's) of the set, from an answer-key run
  they pick (by default the latest v2 run with the backfill's question version); for an exact field
  also the **costly errors** (a decided value on the wrong side of its boundary: a person taken
  for a machine), and the top confusions;
- on the **archive**: how many messages would get a value, from the backfill's proposals;
- a few messages just over and just under the line, to open in the reading pane.

Nothing is computed per slider move on the server. state() sends the answer key's cases (a few
hundred per field) and, per field, a histogram of the archive's proposals in buckets of 0.05
(`bucket`), so the page recomputes every number itself. The histograms are one grouped pass over
the proposals (about a million rows, a second or two), kept in memory until the proposals or
what is accepted change. Only the examples ask the server, by index, when the slider stops.

apply() is backfill.accept() with the thresholds shown (margin 0.15 for the exact fields), over
the backfill and focused runs; the page asks for a confirmation first. What is applied now is
read from the runs (model_run.params.accept, written by accept).
"""

from __future__ import annotations

import threading
from collections import Counter

import psycopg

from talos import backfill, boundary, gold, jev_gold

EXACT = ("origin", "type", "topic", "value")
FIELDS = boundary.FIELDS + (boundary.KIND,) + EXACT
BUCKETS = 20                          # 0.00–0.05, …, 0.95–1.00
SLIDER = (0.50, 0.95)
EXAMPLES = 5

_LOCK = threading.Lock()
_CACHE: dict[tuple, dict] = {}


def bucket(p: float | None) -> int:
    """The histogram bucket of a probability: 0.70 is the first of 0.70–0.75 (as accept counts it)."""
    return min(BUCKETS - 1, max(0, int((p or 0.0) * BUCKETS + 1e-6)))


def archive_runs(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("select id, purpose, created_at, params->>'stage' as stage, coalesce(params->'focus'->>'field', (select string_agg(x, '+')"
                        " from jsonb_array_elements_text(params->'focus'->'fields') x)) as focus,"
                        " params->>'question_version' as question_version, params->'accept' as accept"
                        " from model_run where purpose = any(%s) order by created_at, id",
                        (list(backfill.PURPOSES),)).fetchall()


def gold_runs(conn: psycopg.Connection) -> list[dict]:
    """The answer-key runs of the full question set (focused ones answer one field only)."""
    return conn.execute("select id, created_at, (params->>'set_id')::int as set_id, params->>'unit' as unit,"
                        " params->>'question_version' as question_version,"
                        " coalesce((params->>'template_version')::int, 1) as template_version"
                        " from model_run where purpose = %s and params->'focus' is null"
                        " order by created_at desc, id desc", (jev_gold.PURPOSE,)).fetchall()


def default_gold_run(conn: psycopg.Connection, runs: list[dict] | None = None,
                     archive: list[dict] | None = None) -> str | None:
    """The latest v2 answer-key run asked the backfill's question version, else the latest v2."""
    runs = gold_runs(conn) if runs is None else runs
    archive = archive_runs(conn) if archive is None else archive
    qv = next((r["question_version"] for r in reversed(archive) if r["purpose"] == backfill.PURPOSE), None)
    v2 = [r for r in runs if r["template_version"] >= 2]
    same = [r for r in v2 if qv and r["question_version"] == qv]
    pick = same or v2 or runs
    return pick[0]["id"] if pick else None


def _labels(conn: psycopg.Connection) -> dict:
    """Per field, each value's label; per exact field, each value's boundary side."""
    rows = conn.execute("select id, label, value_meta from dimension where id = any(%s)", (list(FIELDS),)).fetchall()
    return {r["id"]: {"label": r["label"], "values": {v: (m or {}).get("label", v)
                                                      for v, m in (r["value_meta"] or {}).items()}} for r in rows}


# ---------------------------------------------------------------- the answer key

def gold_cases(conn: psycopg.Connection, run_id: str, sd: dict) -> dict:
    """Per field, one row per answer-key item labelled for sure: the item's position and message,
    what Jev said (p), its probability (c) and margin (m), the reference (r); for an exact field
    also the sides (ps, rs) and Jev's own boundary for the item (bs: its side, bp: its
    probability), so the page can apply the agreement rule itself. Kind is derived from the type
    scores; its reference is the set's own kind answer where it has one, else the type answer's kind."""
    run = conn.execute("select params from model_run where id = %s and purpose = %s",
                       (run_id, jev_gold.PURPOSE)).fetchone()
    if not run:
        raise ValueError(f"no answer-key run {run_id!r}")
    set_id = run["params"]["set_id"]
    ref, _ = gold.combined_labels(conn, set_id)
    items = {r["id"]: r for r in conn.execute("select id, position, message_id from gold_item where set_id = %s",
                                              (set_id,))}
    preds: dict[int, dict] = {}
    for r in conn.execute("select item_id, field, top, scores, confidence, margin, raw from jev_prediction"
                          " where run_id = %s and field = any(%s)", (run_id, list(EXACT))):
        preds.setdefault(r["item_id"], {})[r["field"]] = r
    out: dict[str, list] = {f: [] for f in FIELDS if f != boundary.KIND or boundary.KIND in sd}
    for item_id, fields in preds.items():
        it = items.get(item_id)
        if not it:
            continue
        labels = ref.get(item_id, {})
        sides = {}
        for b in [b for b in boundary.DERIVED if b in sd]:
            src = fields.get(boundary.SOURCE[b])
            if src is None:
                continue
            probs = boundary.side_probabilities(src["scores"], sd[b]["side_of"], sd[b]["sides"])
            top, p, m, _ = boundary.decide(probs)
            sides[b] = (top, p, m)
            own = labels.get(b)
            g = own if own is not None else labels.get(boundary.SOURCE[b])
            if g and g["status"] == "set" and g["values"]:
                out[b].append({"i": it["position"], "id": it["message_id"], "p": top, "c": round(p, 4),
                               "m": round(m, 4), "r": g["values"][0] if own is not None
                               else sd[b]["side_of"].get(g["values"][0])})
        for f in EXACT:
            r = fields.get(f)
            g = labels.get(f)
            fixed = r is not None and isinstance(r["raw"], dict) and "fixed" in r["raw"]
            if r is None or fixed or not g or g["status"] != "set" or not g["values"]:
                continue
            b = boundary.OF[f]
            side_of = sd[b]["side_of"]
            bs = sides.get(b, (None, 0.0, 0.0))
            out[f].append({"i": it["position"], "id": it["message_id"], "p": r["top"],
                           "c": round(r["confidence"] or 0.0, 4), "m": round(r["margin"] or 0.0, 4),
                           "r": g["values"][0], "ps": side_of.get(r["top"]), "rs": side_of.get(g["values"][0]),
                           "bs": bs[0], "bp": round(bs[1], 4)})
    for f in out:
        out[f].sort(key=lambda x: x["i"])
    return {"run_id": run_id, "set_id": set_id, "fields": out}


# ---------------------------------------------------------------- the archive

def _fingerprint(conn: psycopg.Connection, runs: list[str]) -> tuple:
    """What changes when proposals or acceptance change: the newest assignment id, and every
    run's accept stamp."""
    a = conn.execute("select max(id) as id from assignment").fetchone()["id"]
    stamps = conn.execute("select string_agg(coalesce(params->'accept'->>'at', '') || id, ',' order by id) as s"
                          " from model_run where id = any(%s)", (runs,)).fetchone()["s"]
    return (tuple(runs), a, stamps)


def histograms(conn: psycopg.Connection, runs: list[str], *, refresh: bool = False) -> dict:
    """Per field: the runs' proposals (proposed or active, not superseded) by probability bucket
    (all, and those with a margin of at least ACCEPT_MARGIN), how many are active now, and the
    messages they cover. Cached until the fingerprint changes."""
    key = _fingerprint(conn, runs)
    with _LOCK:
        if not refresh and key in _CACHE:
            return _CACHE[key]
    out = {f: {"all": [0] * BUCKETS, "margin_ok": [0] * BUCKETS, "active": 0, "total": 0} for f in FIELDS}
    rows = conn.execute(
        "select dimension_id as f, least(%(n)s - 1, greatest(0, floor(coalesce(confidence, 0) * %(n)s + 1e-6)))::int as b,"
        " coalesce((evidence ->> 'margin')::float, 0) >= %(m)s as mok, status = 'active' as active, count(*)::int as n"
        " from assignment where source_kind = 'model' and source_ref = any(%(runs)s)"
        " and status in ('proposed', 'active') and dimension_id = any(%(fields)s)"
        " group by 1, 2, 3, 4", {"n": BUCKETS, "m": backfill.ACCEPT_MARGIN - 1e-6, "runs": runs,
                                 "fields": list(FIELDS)}).fetchall()
    for r in rows:
        h = out[r["f"]]
        h["all"][r["b"]] += r["n"]
        if r["mok"]:
            h["margin_ok"][r["b"]] += r["n"]
        h["total"] += r["n"]
        if r["active"]:
            h["active"] += r["n"]
    with _LOCK:
        _CACHE.clear()
        _CACHE[key] = out
    return out


def applied(archive: list[dict]) -> dict:
    """What accept last applied, per run (model_run.params.accept), and the thresholds of the
    latest one."""
    per = [{"run": r["id"], "stage": r["stage"] or (f"focus {r['focus']}" if r["focus"] else None), **r["accept"]}
           for r in archive if r.get("accept")]
    latest = max(per, key=lambda x: x.get("at") or "", default=None)
    return {"runs": per, "thresholds": (latest or {}).get("thresholds") or {},
            "margin": (latest or {}).get("margin"), "at": (latest or {}).get("at")}


def state(conn: psycopg.Connection, gold_run: str | None = None, *, refresh: bool = False) -> dict:
    """Everything the page needs to recompute its numbers itself."""
    sd = boundary.sides(conn)
    archive = archive_runs(conn)
    runs = [r["id"] for r in archive]
    gruns = gold_runs(conn)
    gold_run = gold_run or default_gold_run(conn, gruns, archive)
    labels = _labels(conn)
    fields = []
    for f in FIELDS:
        if f == boundary.KIND and f not in sd:
            continue  # a taxonomy from before kind
        d = f in boundary.DERIVED
        fields.append({"id": f, "label": labels.get(f, {}).get("label", f),
                       "kind": "kind" if f == boundary.KIND else "boundary" if d else "exact",
                       "boundary": None if d else boundary.OF[f], "from": boundary.SOURCE.get(f),
                       "default": backfill.TWO_LEVEL[f], "values": labels.get(f, {}).get("values", {}),
                       "sides": sd[f]["labels"] if d else None})
    return {"fields": fields, "slider": SLIDER, "bucket": 1 / BUCKETS, "margin": backfill.ACCEPT_MARGIN,
            "defaults": backfill.TWO_LEVEL,
            "runs": [{k: r[k] for k in ("id", "purpose", "created_at", "stage", "focus")} for r in archive],
            "gold_runs": gruns, "gold_run": gold_run,
            "gold": gold_cases(conn, gold_run, sd) if gold_run else None,
            "archive": histograms(conn, runs, refresh=refresh) if runs else {},
            "messages": conn.execute("select count(*)::int as n from message").fetchone()["n"],
            "applied": applied(archive)}


def examples(conn: psycopg.Connection, field: str, threshold: float, *, n: int = EXAMPLES) -> dict:
    """The proposals nearest the threshold: n just over it (they would be accepted) and n just
    under (they would not), each with its message, by index (assignment_model_confidence_idx)."""
    if field not in FIELDS:
        raise ValueError(f"field is one of {', '.join(FIELDS)}")
    runs = [r["id"] for r in archive_runs(conn)]
    labels = _labels(conn).get(field, {}).get("values", {})
    margin = "" if field in boundary.DERIVED else " and coalesce((a.evidence ->> 'margin')::float, 0) >= %(m)s"
    cols = ("a.entity_id as id, a.value, a.confidence, (a.evidence ->> 'margin')::float as margin, a.status,"
            " m.subject, m.from_name, m.from_address, m.received_at, m.medium, m.account_id")
    base = ("from assignment a join message m on m.id = a.entity_id where a.source_kind = 'model'"
            " and a.status in ('proposed', 'active') and a.dimension_id = %(f)s and a.source_ref = any(%(runs)s)")
    p = {"f": field, "t": threshold - 1e-6, "runs": runs, "n": n, "m": backfill.ACCEPT_MARGIN - 1e-6}
    over = conn.execute(f"select {cols} {base} and a.confidence >= %(t)s{margin}"
                        f" order by a.confidence, a.id limit %(n)s", p).fetchall()
    under = conn.execute(f"select {cols} {base} and a.confidence < %(t)s"
                         f" order by a.confidence desc, a.id limit %(n)s", p).fetchall()
    for r in over + under:
        r["label"] = labels.get(r["value"], r["value"])
    return {"field": field, "threshold": threshold, "over": over, "under": under}


def apply(conn: psycopg.Connection, thresholds: dict, *, dry_run: bool = False) -> dict:
    """backfill.accept() over every backfill and focused run with these thresholds (the nine
    fields, each from 0.5 to 0.99; exact fields with a margin of 0.15)."""
    if not isinstance(thresholds, dict) or not thresholds:
        raise ValueError("thresholds: a threshold per field")
    clean = {}
    for f, t in thresholds.items():
        if f not in FIELDS:
            raise ValueError(f"no field {f!r}; the fields are {', '.join(FIELDS)}")
        try:
            t = round(float(t), 4)
        except (TypeError, ValueError):
            raise ValueError(f"the threshold of {f} is a number") from None
        if not 0.5 <= t <= 0.99:
            raise ValueError(f"the threshold of {f} is from 0.50 to 0.99")
        clean[f] = t
    runs = [r["id"] for r in archive_runs(conn)]
    if not runs:
        raise ValueError("no backfill run to accept")
    res = backfill.accept(conn, runs, clean, margin=backfill.ACCEPT_MARGIN, dry_run=dry_run)
    with _LOCK:
        _CACHE.clear()
    return {k: res[k] for k in ("runs", "decided_by", "dry_run")} | {
        "fields": {f: {k: d[k] for k in ("threshold", "promoted", "demoted", "active", "still_proposed", "held_back")}
                   for f, d in res["fields"].items()}}


def score(cases: list[dict], threshold: float, *, margin: float | None = None,
          boundary_threshold: float | None = None) -> dict:
    """One field on the answer key at a threshold, as the page computes it: of the items labelled
    for sure, how many are decided (probability ≥ threshold; an exact field also margin ≥ margin
    and, two-level, its side agreeing with its own boundary where that is decided at
    boundary_threshold), how many of those are right, the costly errors (an exact value on the
    wrong side of its boundary) and the confusions ('Jev→reference')."""
    decided = right = costly = 0
    wrong: Counter = Counter()
    for x in cases:
        ok = x["c"] >= threshold - 1e-9
        if margin is not None:
            ok = ok and x["m"] >= margin - 1e-9
        if ok and boundary_threshold is not None and x.get("bs") is not None and x["bp"] >= boundary_threshold - 1e-9:
            ok = x["bs"] == x.get("ps")
        if not ok:
            continue
        decided += 1
        if x["p"] == x["r"]:
            right += 1
        else:
            wrong[f"{x['p']}→{x['r']}"] += 1
            if x.get("ps") is not None and x.get("rs") is not None and x["ps"] != x["rs"]:
                costly += 1
    n = len(cases)
    return {"n": n, "decided": decided, "right": right, "costly": costly, "coverage": gold._rate(decided, n),
            "accuracy": gold._rate(right, decided), "confusions": dict(wrong.most_common(5))}
