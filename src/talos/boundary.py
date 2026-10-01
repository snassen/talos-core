"""The four boundaries of two-level acceptance: the lines that are costly to get wrong.

docs/enrichment-plan.md §11. Not every mistake costs the same: taking an alert for a
notification is fine, taking a person who asks the owner something for a machine is not.
So besides the exact values (origin, type, topic, value) Talos keeps four coarse, two-sided
dimensions, each derived from one exact field by the taxonomy's families
(rules/taxonomy.json `derived_from`, loaded into `dimension.derived_from`):

| Boundary      | From   | Sides                                                             |
|---------------|--------|-------------------------------------------------------------------|
| `sender_kind` | origin | people (the People family: person, person_via_system, list) / machine |
| `sphere`      | topic  | work (the Work/ family) / personal                                |
| `form`        | type   | conversation (the Conversation family) / other                    |
| `keep`        | value  | keep (Keep and find, Records) / short_lived (Short-lived)         |

**A side's probability** is the sum of Jev's probabilities of the source values on that side,
from the full scores stored per case (enrich_prediction.scores), not the top three written into
the proposals' evidence. A side is decided at BOUNDARY_MIN (0.90). A Teams window's origin is not
asked; its sender_kind is `people`, fixed (probability 1).

**Kind** (docs/mailbox-structure-plan.md, decision 7) is derived the same way, from type, but by
value rather than by family (every type is listed under one of eleven kinds: `derived_from.values`)
and with more than two sides. Its probability is the sum of the probabilities of the types under
it, from the same full scores; it is decided at KIND_MIN (0.85, chosen on the answer key: see
kind_threshold in docs). It is not a boundary of two-level acceptance: an exact type is not held
back by form alone but also by an accepted kind of another side (backfill.accept). DERIVED is the
four boundaries and kind: everything proposed from a source field's scores.

Everything here reads the mapping from the database, so the SQL and Python agree with whatever
`talos taxonomy load` last wrote.
"""

from __future__ import annotations

import psycopg

from talos import taxonomy

FIELDS = ("sender_kind", "sphere", "form", "keep")
KIND = "kind"
DERIVED = FIELDS + (KIND,)
SOURCE = {"sender_kind": "origin", "sphere": "topic", "form": "type", "keep": "value", KIND: "type"}
OF = {SOURCE[b]: b for b in FIELDS}             # exact field → its boundary
BOUNDARY_MIN = 0.90
KIND_MIN = 0.85
EXACT_MIN = 0.70


def threshold(field: str) -> float:
    """The line at which a derived field is decided."""
    return KIND_MIN if field == KIND else BOUNDARY_MIN


class BoundaryError(ValueError):
    pass


def sides(conn: psycopg.Connection) -> dict[str, dict]:
    """{derived field: {"from": field, "sides": [side, …], "side_of": {source value: side}}}, from
    the loaded taxonomy: the four boundaries, and kind once it is loaded. Raises BoundaryError when
    a boundary or its source has no list yet."""
    rows = {r["id"]: r for r in conn.execute(
        "select id, allowed, value_meta, derived_from from dimension where id = any(%s)",
        (list(DERIVED) + list(SOURCE.values()),))}
    out = {}
    for b in DERIVED:
        d = rows.get(b)
        src = SOURCE[b]
        if not d or not d["derived_from"] or not d["allowed"] or not (rows.get(src) or {}).get("allowed"):
            if b == KIND:
                continue  # kind is optional: a taxonomy from before it has none
            raise BoundaryError(f"the boundary {b} has no mapping yet; load the taxonomy first: talos taxonomy load")
        meta = rows[src]["value_meta"] or {}
        side_of = taxonomy.mapping(d["derived_from"], meta, rows[src]["allowed"])
        missing = sorted(v for v, s in side_of.items() if s is None)
        if missing:
            raise BoundaryError(f"{b}: these {src} values map to no side: {', '.join(missing)}")
        out[b] = {"from": src, "sides": list(d["allowed"]), "side_of": side_of,
                  "labels": {v: ((d["value_meta"] or {}).get(v) or {}).get("label", v) for v in d["allowed"]}}
    return out


def side_probabilities(scores: dict[str, float], side_of: dict[str, str], all_sides=()) -> dict[str, float]:
    """The sum of the probabilities of each side's values. A value with no side is left out."""
    out = {s: 0.0 for s in all_sides}
    for v, p in (scores or {}).items():
        s = side_of.get(v)
        if s is not None:
            out[s] = out.get(s, 0.0) + float(p)
    return {s: round(p, 6) for s, p in out.items()}


def decide(probs: dict[str, float], threshold: float = BOUNDARY_MIN) -> tuple[str | None, float, float, bool]:
    """(top side, its probability, margin over the other side, decided at threshold)."""
    if not probs:
        return None, 0.0, 0.0, False
    ranked = sorted(probs.items(), key=lambda kv: (-kv[1], kv[0]))
    top, p = ranked[0]
    runner = ranked[1][1] if len(ranked) > 1 else 0.0
    return top, p, round(p - runner, 6), p >= threshold - 1e-9


def value_sides(sd: dict[str, dict]) -> tuple[list, list, list, list]:
    """The mapping as four parallel arrays (boundary, field, value, side), for SQL's unnest()."""
    b, f, v, s = [], [], [], []
    for bd, d in sd.items():
        for value, side in d["side_of"].items():
            b.append(bd)
            f.append(d["from"])
            v.append(value)
            s.append(side)
    return b, f, v, s


# The derived predictions (boundaries and kind) of a run's cases, from the stored full scores of
# the source fields (a fixed Teams origin gives sender_kind people at 1.0); the margin is the top
# side over the next one. Columns as enrich_prediction's, plus derived_from. Parameters: %(runs)s (run ids) and the four arrays of value_sides() as %(vs_b)s, %(vs_f)s,
# %(vs_v)s, %(vs_s)s. A case that has a stored prediction for the boundary (a focused run that
# asked it) is left out: the stored one counts.
DERIVED_SQL = """
    select d.case_id, d.bdim as field, d.top, d.scores, d.top_p as confidence,
           round((d.top_p - coalesce(d.runner_p, 0))::numeric, 6)::float as margin, d.derived_from, d.fixed
    from (select sp.case_id, sp.bdim, max(sp.src) as derived_from, bool_or(sp.fixed) as fixed,
                 (array_agg(sp.side order by sp.p desc, sp.side))[1] as top, max(sp.p) as top_p,
                 (array_agg(sp.p order by sp.p desc, sp.side))[2] as runner_p,
                 jsonb_object_agg(sp.side, round(sp.p::numeric, 4)) as scores
          from (select p.case_id, vs.bdim, vs.side, vs.src, bool_or(p.fixed) as fixed,
                       round(sum((sc.value)::numeric), 6)::float as p
                from enrich_case c join enrich_prediction p on p.case_id = c.id
                cross join lateral jsonb_each_text(p.scores) sc
                join unnest(%(vs_b)s::text[], %(vs_f)s::text[], %(vs_v)s::text[], %(vs_s)s::text[])
                     as vs(bdim, src, value, side) on vs.src = p.field and vs.value = sc.key
                where c.run_id = any(%(runs)s) and c.error is null
                group by 1, 2, 3, 4) sp
          group by 1, 2) d
    where not exists (select 1 from enrich_prediction s where s.case_id = d.case_id and s.field = d.bdim)"""


def params(sd: dict[str, dict]) -> dict:
    b, f, v, s = value_sides(sd)
    return {"vs_b": b, "vs_f": f, "vs_v": v, "vs_s": s}
