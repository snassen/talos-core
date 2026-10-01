"""The Studio: quick decisions that settle mail by the group, and lift Jev where it proves right.

docs/studio.md. Like naming faces in a photo library: Talos shows one card at a time, a group of
mail Jev is unsure of, with Jev's guess for each quality already ticked. The owner confirms with one
key, or ticks another value. The answer is written as the owner's own decision on every message of
the group,
and a counter shows how many messages it settled.

**Groups.** Machine mail groups by its subject pattern (sender and subject skeleton,
`message_pattern`): one system's same notice, however many. People's mail groups by thread. A
card's group is the incoming e-mail of that pattern or thread; the owner can narrow a decision to
the one message shown.

**The lines** of a card are the qualities in FIELDS: people or machine, kind, topic and value.
Each shows the group's current value (the owner's, a rule's, Jev's accepted value, or Jev's proposal where
nothing counts yet), how sure it is (talos.sureness), and the alternatives Jev weighed. Writing a
topic or a value also writes its boundary side as the owner's (sphere, keep), as the answer-key groups do.

**The pool** (`pool()`): every incoming e-mail with, per field, whether it is settled (the owner's or
a rule's value, or Jev's accepted one at Confident or surer), in groups ranked by how many unsettled
field-values one card would settle. About 8 s on the real archive, so it lives in insight_cache and
is recomputed behind the page when the values change. A group the owner decided (or skipped in the last
SKIP_DAYS days) is not shown again.

**Calibration and lifts.** Every line the owner decides whose representative had a Jev value is a
verdict on Jev in a *cell*: the field, Jev's value, its level and people-or-machine
(`studio_verdict`). Once a cell has LIFT_MIN verdicts on proposals and the lower bound of its
agreement (Wilson, one-sided 95%) is at least LIFT_FLOOR, the cell's other proposals are
accepted: a *lift*, recorded in `studio_lift` with every row it accepted, undoable. A lifted
value's confidence is the calibrated one, so its level says how sure it really is. Lifted cells
take new mail's proposals too (`relift()`, after each sync), so new mail gets the same treatment.

**Check cards.** Now and then the feed shows a card chosen for calibration rather than size: a
random message from the cell closest to a lift (most messages per verdict still needed), from a
sender the cell has not seen yet, so the verdicts are a fair sample.

The owner's decisions are human values: they beat everything and are never touched by a rerun of Jev.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

from talos import boundary, insight, personal, search, structure, sureness

IN_TRASH = search._IN_TRASH  # a location in a trash folder (the red chip's test)

FIELDS = ("sender_kind", "kind", "topic", "value")
LABELS = {"sender_kind": "People or machine", "kind": "Kind", "topic": "Topic", "value": "Value"}
SETTLED = sureness.FLOOR["confident"]   # a value this sure (or the owner's, or a rule's) needs no card
POOL = "studio-pool"
POOL_KEEP = 5000                        # groups kept in the cache
SAMPLE = 400                            # a group's newest messages read to show its lines
LIFT_MIN = 12                           # verdicts a cell needs before it can lift
LIFT_FLOOR = sureness.FLOOR["confident"]
Z = 1.645                               # one-sided 95%
CHECK_EVERY = 3                         # every third card a check card, when a cell is worth it
CELL_MIN = 50                           # a cell smaller than this is not worth checking
NEED_MAX = 40                           # a cell further than this from a lift is not offered
SKIP_DAYS = 7
MAX_GROUP = 50_000


class StudioError(ValueError):
    pass


# ---------------------------------------------------------------- statistics

def wilson_lower(agreed: int, n: int, z: float = Z) -> float:
    """The lower bound of the agreement rate: how right Jev is at least, given the owner's verdicts."""
    if n <= 0:
        return 0.0
    p = agreed / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - spread) / d)


def need(agreed: int, n: int) -> int | None:
    """How many more agreeing verdicts a cell needs to lift (None: more than NEED_MAX)."""
    for k in range(NEED_MAX + 1):
        if n + k >= LIFT_MIN and wilson_lower(agreed + k, n + k) >= LIFT_FLOOR - 1e-9:
            return k
    return None


# ---------------------------------------------------------------- the pool

_STATE_SQL = """
    select a.entity_id, a.dimension_id as f,
           bool_or(a.status = 'active' and a.source_kind in ('human', 'rule')) as hard,
           max(a.confidence) filter (where a.status = 'active' and a.source_kind = 'model') as act,
           bool_or(a.status = 'active') as any_active,
           (array_agg(a.value order by (a.status = 'active') desc, case a.source_kind when 'human' then 4
              when 'rule' then 3 when 'model' then 2 else 1 end desc, a.confidence desc nulls last))[1] as best,
           (array_agg(a.confidence order by (a.status = 'active') desc, case a.source_kind when 'human' then 4
              when 'rule' then 3 when 'model' then 2 else 1 end desc, a.confidence desc nulls last))[1] as conf
    from assignment a
    where a.dimension_id = any(%(fields)s) and a.status in ('active', 'proposed')
    group by 1, 2"""


def pool_fingerprint(conn: psycopg.Connection) -> str:
    r = conn.execute("select max(id) as a, count(*) filter (where status = 'active') as n from assignment"
                     " where source_kind = 'human' or decided_by like 'studio%%' or source_ref like 'studio%%'"
                     ).fetchone()
    last = conn.execute("select max(id) as m from assignment").fetchone()["m"]
    # Jev's runs and accepts move many rows at once: the newest row id, in steps, says so cheaply.
    return insight.fingerprint([r["a"], r["n"], (last or 0) // 5000,
                                datetime.now(timezone.utc).strftime("%Y-%m-%d")])


def compute_pool(conn: psycopg.Connection) -> dict:
    """Groups ranked by the unsettled field-values one decision would settle, and the cells of
    Jev's proposals (field, value, level, side) with how many messages each holds."""
    t0 = time.monotonic()
    with conn.transaction():
        conn.execute("set local work_mem = '256MB'")
        conn.execute("drop table if exists studio_x")
        conn.execute(f"""
            create temp table studio_x on commit drop as
            with loc as materialized (
                select l.message_id, bool_or(not {IN_TRASH}) as live
                from message_location l where l.present group by 1),
            m as materialized (
                select m.id, m.thread_id, m.is_automated, p.pattern_key as pk, m.received_at,
                       coalesce(loc.live, false) as live
                from message m left join message_pattern p on p.message_id = m.id
                left join loc on loc.message_id = m.id
                where m.medium = 'email' and m.direction = 'in'),
            st as materialized ({_STATE_SQL})
            select m.id, m.thread_id, m.pk, m.received_at, m.live, f.f,
                   coalesce(s.hard, false) or coalesce(t.hard, false) or coalesce(s.act, 0) >= %(settled)s as settled,
                   not coalesce(s.any_active, false) and not coalesce(t.any_active, false) as open,
                   s.best, s.conf,
                   coalesce(sk.best = 'machine', m.is_automated) as machine
            from m cross join unnest(%(fields)s::text[]) f(f)
            left join st s on s.entity_id = m.id and s.f = f.f
            left join st t on t.entity_id = m.thread_id and t.f = f.f
            left join st sk on sk.entity_id = m.id and sk.f = 'sender_kind'""",
                     {"fields": list(FIELDS), "settled": SETTLED - 1e-9})
        # sender_kind decides the side for every field of a message: read it once per message
        conn.execute("""create temp table studio_side on commit drop as
                        select distinct on (id) id, machine from studio_x""")
        conn.execute("create index on studio_side (id)")
        conn.execute("analyze studio_x; analyze studio_side")
        groups = conn.execute(f"""
            select g.gk as key, count(distinct g.id)::int as messages,
                   {", ".join(f"count(*) filter (where g.f = '{f}' and not g.settled)::int as u_{f}" for f in FIELDS)},
                   count(*) filter (where not g.settled)::int as score,
                   count(*) filter (where not g.settled and g.live)::int as live_score,
                   count(*) filter (where not g.settled and g.live and g.received_at > now() - interval '90 days')::int
                       as recent_score,
                   count(distinct g.id) filter (where g.live)::int as live,
                   (array_agg(g.id order by g.settled, g.received_at desc nulls last, g.id desc))[1] as rep,
                   bool_or(g.machine) as machine
            from (select x.*, case when s.machine and x.pk is not null then 'p:' || x.pk
                                   when not s.machine and x.thread_id is not null then 't:' || x.thread_id
                                   else 'm:' || x.id end as gk, s.machine as side_machine
                  from studio_x x join studio_side s on s.id = x.id) g
            group by g.gk having count(*) filter (where not g.settled) > 0
            order by count(*) filter (where not g.settled and g.live)
                     + 2 * count(*) filter (where not g.settled and g.live and g.received_at > now() - interval '90 days') desc,
                     count(*) filter (where not g.settled) desc, count(distinct g.id) desc limit %(keep)s""",
            {"keep": POOL_KEEP}).fetchall()
        cells = conn.execute(f"""
            select x.f as field, x.best as value, {sureness.level_sql("'model'", 'x.conf')} as level,
                   case when s.machine then 'machine' else 'people' end as side, count(*)::int as messages
            from studio_x x join studio_side s on s.id = x.id
            where x.open and x.best is not null
            group by 1, 2, 3, 4 having count(*) >= %(min)s order by 5 desc""", {"min": CELL_MIN}).fetchall()
        totals = conn.execute("select f, count(*) filter (where not settled)::int as unsettled, count(*)::int as n"
                              " from studio_x group by f").fetchall()
    for g in groups:
        g["unsettled"] = {f: g.pop(f"u_{f}") for f in FIELDS}
    return {"groups": groups,
            "cells": cells, "totals": {r["f"]: {"unsettled": r["unsettled"], "messages": r["n"]} for r in totals},
            "seconds": round(time.monotonic() - t0, 2)}


def pool(conn: psycopg.Connection, *, dsn: str | None = None, refresh: bool = False) -> dict:
    row = insight.get(conn, POOL, pool_fingerprint, compute_pool, dsn=dsn, refresh=refresh, first_behind=True)
    p = row["payload"] or {"groups": [], "cells": [], "totals": {}}
    return {**p, "computed_at": row["computed_at"], "stale": row["stale"], "refreshing": row["refreshing"]}


# ---------------------------------------------------------------- a group and its card

def group_ids(conn: psycopg.Connection, key: str) -> list[int]:
    """The incoming e-mail of a group, newest first: a pattern's, a thread's, or one message."""
    if not isinstance(key, str) or ":" not in key:
        raise StudioError("a group key")
    kind, rest = key.split(":", 1)
    base = "select m.id from message m {j} where m.medium = 'email' and m.direction = 'in' and {w}" \
           " order by m.received_at desc nulls last, m.id desc limit %s"
    if kind == "p":
        q = base.format(j="join message_pattern p on p.message_id = m.id", w="p.pattern_key = %s")
    elif kind == "t" and rest.isdigit():
        q, rest = base.format(j="", w="m.thread_id = %s"), int(rest)
    elif kind == "m" and rest.isdigit():
        q, rest = base.format(j="", w="m.id = %s"), int(rest)
    else:
        raise StudioError(f"not a group key: {key!r}")
    return [r["id"] for r in conn.execute(q, (rest, MAX_GROUP))]


def group_of(conn: psycopg.Connection, message_id: int) -> str:
    """The group a message belongs to, as the pool groups it."""
    r = conn.execute(f"select m.id, m.thread_id, p.pattern_key, {search.machine_sql('m')} as machine"
                     " from message m left join message_pattern p on p.message_id = m.id where m.id = %s",
                     (message_id,)).fetchone()
    if not r:
        raise StudioError(f"no message {message_id}")
    if r["machine"] and r["pattern_key"]:
        return "p:" + r["pattern_key"]
    if not r["machine"] and r["thread_id"]:
        return f"t:{r['thread_id']}"
    return f"m:{message_id}"


def options(conn: psycopg.Connection) -> dict:
    """Each field's values in the taxonomy's order, with label, family and description."""
    out = {}
    for d in conn.execute("select id, allowed, value_meta from dimension where id = any(%s)", (list(FIELDS),)):
        meta = d["value_meta"] or {}
        values = list(d["allowed"] or [])
        if not values:
            values = [r["value"] for r in conn.execute(
                "select value from assignment where dimension_id = %s and status = 'active'"
                " group by 1 order by count(*) desc limit 200", (d["id"],))]
        out[d["id"]] = [{"value": v, "label": (meta.get(v) or {}).get("label", v),
                         "family": (meta.get(v) or {}).get("family", ""),
                         "description": (meta.get(v) or {}).get("description", "")} for v in values]
    return out


def _states(conn: psycopg.Connection, ids: list[int]) -> dict[tuple[int, str], dict]:
    """Per message and field: the value that counts (with its source and level), else Jev's
    newest proposal; and whether it is settled."""
    out: dict[tuple[int, str], dict] = {}
    # A lateral lookup per message: the view is read by index for each one. Joined to a list of ids
    # instead, the planner reads the whole view (minutes on the real archive).
    for r in conn.execute(
            "select x.id as message_id, e.dimension_id, e.value, e.source_kind, e.confidence from unnest(%s::bigint[]) x(id)"
            " cross join lateral (select * from effective_message_assignment v where v.message_id = x.id"
            " and v.dimension_id = any(%s)) e", (ids, list(FIELDS))):
        out[(r["message_id"], r["dimension_id"])] = {
            "value": r["value"], "source": r["source_kind"], "status": "active", "confidence": r["confidence"],
            "level": sureness.level_of(r["source_kind"], r["confidence"]),
            "settled": r["source_kind"] in ("human", "rule") or sureness.confidence_of(
                r["source_kind"], r["confidence"]) >= SETTLED - 1e-9}
    for r in conn.execute(
            "select distinct on (a.entity_id, a.dimension_id) a.entity_id, a.dimension_id, a.value, a.confidence"
            " from assignment a where a.entity_id = any(%s) and a.dimension_id = any(%s) and a.status = 'proposed'"
            " and a.source_kind = 'model' order by a.entity_id, a.dimension_id, a.confidence desc nulls last",
            (ids, list(FIELDS))):
        out.setdefault((r["entity_id"], r["dimension_id"]), {
            "value": r["value"], "source": "model", "status": "proposed", "confidence": r["confidence"],
            "level": sureness.level_of("model", r["confidence"]), "settled": False})
    return out


def _jev_value(conn: psycopg.Connection, message_id: int, field: str) -> dict | None:
    """The message's own Jev value for a field: its accepted one, else its newest proposal; with
    the scores Jev weighed (evidence.scores)."""
    return conn.execute(
        "select value, confidence, status, evidence->'scores' as scores from assignment"
        " where entity_id = %s and dimension_id = %s and source_kind = 'model' and status in ('active', 'proposed')"
        " order by (status = 'active') desc, created_at desc, id desc limit 1", (message_id, field)).fetchone()


def _is_machine(conn: psycopg.Connection, message_id: int) -> bool:
    r = conn.execute(f"select {search.machine_sql('m')} as x from message m where m.id = %s", (message_id,)).fetchone()
    return bool(r and r["x"])


def card(conn: psycopg.Connection, key: str, *, kind: str = "lever", cell: dict | None = None,
         rep_id: int | None = None) -> dict | None:
    """A card for a group: its representative, a few of its subjects, its size, and a line per
    field (None when every field of the group is settled already)."""
    ids = group_ids(conn, key)
    if not ids:
        return None
    sample = ids[:SAMPLE]
    states = _states(conn, sample)
    if rep_id is None or rep_id not in ids:
        unsettled = [i for i in sample if any(not (states.get((i, f)) or {}).get("settled") for f in FIELDS)]
        rep_id = (unsettled or sample)[0]
    lines = []
    for f in FIELDS:
        vals = [states.get((i, f)) for i in sample]
        known = [v for v in vals if v]
        count = Counter(v["value"] for v in known)
        mine = states.get((rep_id, f))
        jev = _jev_value(conn, rep_id, f)
        current = (mine or {}).get("value") or (count.most_common(1)[0][0] if count else None)
        alts = []
        if jev and isinstance(jev["scores"], dict):
            alts = [v for v, _ in sorted(jev["scores"].items(), key=lambda x: -float(x[1] or 0))
                    if v != current and v not in ("none",)]
        alts += [v for v, _ in count.most_common() if v != current and v not in alts]
        settled = sum(1 for v in vals if v and v["settled"])
        lines.append({"field": f, "label": LABELS[f], "value": current,
                      "source": (mine or {}).get("source"), "status": (mine or {}).get("status"),
                      "confidence": (mine or {}).get("confidence"),
                      "level": (mine or {}).get("level") or ("guess" if current else None),
                      "agree": round(count[current] / len(sample), 3) if current else 0.0,
                      "settled": settled, "sampled": len(sample), "done": settled == len(sample),
                      "alternatives": alts[:4],
                      "jev": {"value": jev["value"], "confidence": jev["confidence"], "status": jev["status"],
                              "level": sureness.level_of("model", jev["confidence"])} if jev else None})
    if all(ln["done"] for ln in lines) and kind == "lever":
        return None
    rep = conn.execute(
        "select m.id, m.account_id, m.from_name, m.from_address, m.subject, m.snippet, m.received_at,"
        " m.has_attachments, m.thread_id from message m where m.id = %s", (rep_id,)).fetchone()
    span = conn.execute("select min(received_at) as first, max(received_at) as last, count(distinct subject)::int"
                        " as subjects from message where id = any(%s)", (ids,)).fetchone()
    live = conn.execute(f"select count(distinct l.message_id)::int as n from message_location l"
                        f" where l.message_id = any(%s) and l.present and not {IN_TRASH}", (ids,)).fetchone()["n"]
    rep = {**rep, "removed": conn.execute(f"select {search.REMOVED_COLUMN} from message m where m.id = %s",
                                         (rep_id,)).fetchone()["removed"]}
    others = [r["subject"] for r in conn.execute(
        "select subject from (select distinct on (subject) subject, received_at from message where id = any(%s)"
        " and id <> %s order by subject, received_at desc) x order by received_at desc limit 3", (sample, rep_id))]
    pattern = None
    if key.startswith("p:"):
        pattern = conn.execute("select skeleton from subject_pattern where pattern_key = %s",
                               (key[2:],)).fetchone()
    return {"key": key, "kind": kind, "cell": cell, "messages": len(ids), "live": live, "rep": rep,
            "machine": _is_machine(conn, rep_id), "first": span["first"], "last": span["last"],
            "subjects": span["subjects"], "others": others,
            "pattern": pattern["skeleton"] if pattern else None,
            "group": "pattern" if key.startswith("p:") else "thread" if key.startswith("t:") else "message",
            "lines": lines}


# ---------------------------------------------------------------- the feed

def _decided_keys(conn: psycopg.Connection) -> set[str]:
    return {r["group_key"] for r in conn.execute(
        "select group_key from studio_decision where undone_at is null"
        " and (scope <> 'skip' or at > now() - make_interval(days => %s))", (SKIP_DAYS,))}


def cell_stats(conn: psycopg.Connection) -> dict[tuple, dict]:
    """Per cell: the owner's verdicts on Jev's proposals there (n, agreed), and the senders they came from."""
    out: dict[tuple, dict] = {}
    for r in conn.execute(
            "select v.field, v.value, v.level, v.side, count(*)::int as n, count(*) filter (where v.agreed)::int as a,"
            " array_agg(distinct v.sender) as senders from studio_verdict v join studio_decision d on d.id = v.decision_id"
            " where d.undone_at is null and v.status = 'proposed' group by 1, 2, 3, 4"):
        out[(r["field"], r["value"], r["level"], r["side"])] = {"n": r["n"], "agreed": r["a"],
                                                               "senders": [s for s in r["senders"] if s]}
    return out


def _lifted(conn: psycopg.Connection) -> set[tuple]:
    return {(r["field"], r["value"], r["level"], r["side"]) for r in conn.execute(
        "select distinct field, value, level, side from studio_lift where undone_at is null")}


def check_cells(conn: psycopg.Connection, p: dict) -> list[dict]:
    """The cells worth checking, best first: most messages settled per verdict still needed."""
    stats, lifted = cell_stats(conn), _lifted(conn)
    out = []
    for c in p.get("cells") or []:
        k = (c["field"], c["value"], c["level"], c["side"])
        if k in lifted or c["level"] in ("fact", "certain", "confident"):
            continue
        s = stats.get(k, {"n": 0, "agreed": 0, "senders": []})
        left = need(s["agreed"], s["n"])
        if left is None:
            continue
        out.append({**c, "verdicts": s["n"], "agreed": s["agreed"], "need": left, "senders": s["senders"],
                    "worth": c["messages"] / max(1, left)})
    out.sort(key=lambda c: -c["worth"])
    return out


def _check_card(conn: psycopg.Connection, cell: dict, decided: set[str], exclude: set[str]) -> dict | None:
    """A random message of the cell's proposals, from a sender the cell has not been checked on,
    shown as its group's card with the cell's field in focus."""
    rows = conn.execute(
        f"select a.entity_id as id, lower(m.from_address) as sender from assignment a join message m on m.id = a.entity_id"
        f" where a.dimension_id = %(f)s and a.value = %(v)s and a.status = 'proposed' and a.source_kind = 'model'"
        f" and {sureness.level_sql(chr(39) + 'model' + chr(39), 'a.confidence')} = %(level)s"
        f" and m.medium = 'email' and m.direction = 'in'"
        f" and not exists (select 1 from assignment b where b.entity_id in (m.id, m.thread_id)"
        f"                 and b.dimension_id = %(f)s and b.status = 'active')"
        f" and coalesce(lower(m.from_address), '') <> all(%(seen)s)"
        f" order by random() limit 40",
        {"f": cell["field"], "v": cell["value"], "level": cell["level"], "seen": cell.get("senders") or []}).fetchall()
    for r in rows:
        if _is_machine(conn, r["id"]) != (cell["side"] == "machine"):
            continue
        key = group_of(conn, r["id"])
        if key in decided or key in exclude:
            continue
        c = card(conn, key, kind="check", rep_id=r["id"],
                 cell={k: cell[k] for k in ("field", "value", "level", "side", "messages", "verdicts", "agreed", "need")})
        if c:
            return c
    return None


def feed(conn: psycopg.Connection, *, n: int = 4, exclude: list[str] | None = None, dsn: str | None = None,
         position: int = 0) -> dict:
    """The next cards: the groups that settle the most, with a check card every CHECK_EVERY-th
    place (position counts the cards the owner has seen this session)."""
    p = pool(conn, dsn=dsn)
    decided, skip = _decided_keys(conn), set(exclude or [])
    cells = check_cells(conn, p)
    out, i = [], 0
    groups = iter(p.get("groups") or [])
    checks = iter(cells[:6])
    while len(out) < n:
        pos = position + len(out) + 1
        c = None
        if pos % CHECK_EVERY == 0:
            cell = next(checks, None)
            if cell:
                c = _check_card(conn, cell, decided, skip)
        if c is None:
            g = next(groups, None)
            if g is None:
                break
            i += 1
            if g["key"] in decided or g["key"] in skip:
                continue
            c = card(conn, g["key"], rep_id=g["rep"])
        if c:
            skip.add(c["key"])
            out.append(c)
        if i > 200:
            break
    return {"cards": out, "options": options(conn), "levels": sureness.levels(),
            "cells": cells[:8], "pool": {k: p.get(k) for k in ("computed_at", "stale", "refreshing", "totals")}}


# ---------------------------------------------------------------- deciding

def _sides_of(conn: psycopg.Connection) -> dict[str, tuple[str, dict]]:
    """For a field the owner sets, the boundary whose side follows from it: topic → sphere, value → keep."""
    try:
        sd = boundary.sides(conn)
    except boundary.BoundaryError:
        return {}
    return {src: (b, sd[b]["side_of"]) for b, src in boundary.SOURCE.items()
            if b in sd and src in FIELDS and b != boundary.KIND}


def _write(conn: psycopg.Connection, ids: list[int], dim: str, value: str, ref: str) -> tuple[list[int], list[int]]:
    """The owner's value on each message; earlier human values of a one-value field are superseded."""
    one = conn.execute("select cardinality from dimension where id = %s", (dim,)).fetchone()
    sup = []
    if one and one["cardinality"] == "one":
        sup = [r["id"] for r in conn.execute(
            "update assignment set status = 'superseded', decided_by = %s, decided_at = now()"
            " where entity_id = any(%s) and dimension_id = %s and source_kind = 'human' and status = 'active'"
            " returning id", (ref, ids, dim))]
    new = [r["id"] for r in conn.execute(
        "insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, confidence, decided_by,"
        " decided_at, evidence) select unnest(%s::bigint[]), %s, %s, 'human', %s, 1.0, %s, now(),"
        " jsonb_build_object('studio', true) returning id", (ids, dim, value, ref, personal.OWNER_ID))]
    return new, sup


def decide(conn: psycopg.Connection, key: str, lines: list[dict], *, scope: str = "group", rep_id: int | None = None,
           card_kind: str = "lever") -> dict:
    """The owner's answer to a card. lines: [{field, value}] with value their choice, or null for
    "not this" (Jev's value rejected, nothing written), or the line left out when they did not judge it.
    scope 'group' writes on every message of the group, 'one' on the representative alone."""
    if scope not in ("group", "one"):
        raise StudioError("scope is group or one")
    if card_kind not in ("lever", "check"):
        raise StudioError("card is lever or check")
    ids = group_ids(conn, key)
    if not ids:
        raise StudioError("the group has no messages")
    rep_id = rep_id if rep_id in ids else ids[0]
    scope_ids = ids if scope == "group" else [rep_id]
    opts = options(conn)
    clean = []
    for ln in lines or []:
        f, v = ln.get("field"), ln.get("value")
        if f not in FIELDS:
            raise StudioError(f"a field is one of {', '.join(FIELDS)}")
        if v is not None and opts.get(f) and v not in {o["value"] for o in opts[f]}:
            raise StudioError(f"{v!r} is not a value of {f}")
        clean.append((f, v))
    if not clean:
        raise StudioError("nothing decided")
    decided_fields = [f for f, v in clean if v is not None]
    # messages it settles: those that did not already have the owner's (or a rule's) value for a field set here,
    # on the message or its thread (read from the table by index: a group can be tens of thousands)
    sets = [(f, v) for f, v in clean if v is not None]
    settled = conn.execute(
        "select count(*)::int as n from message m where m.id = any(%(ids)s) and exists ("
        " select 1 from unnest(%(f)s::text[], %(v)s::text[]) fv(f, v) where not exists ("
        "  select 1 from assignment a where a.entity_id in (m.id, m.thread_id) and a.dimension_id = fv.f"
        "  and a.value = fv.v and a.status = 'active' and a.source_kind in ('human', 'rule')))",
        {"ids": scope_ids, "f": [f for f, _ in sets], "v": [v for _, v in sets]}).fetchone()["n"] if sets else 0
    side = "machine" if _is_machine(conn, rep_id) else "people"
    sender = (conn.execute("select lower(from_address) as s from message where id = %s", (rep_id,)).fetchone() or {}).get("s")
    jev = {f: _jev_value(conn, rep_id, f) for f, _ in clean}
    did = conn.execute("insert into studio_decision (card, group_key, scope, rep_id, message_ids, lines)"
                       " values (%s, %s, %s, %s, %s, %s) returning id",
                       (card_kind, key, scope, rep_id, scope_ids, Jsonb([{"field": f, "value": v} for f, v in clean]))
                       ).fetchone()["id"]
    ref = f"studio:{did}"
    written, superseded, rejected = [], [], []
    sides = _sides_of(conn)
    for f, v in clean:
        if v is None:
            j = jev[f]
            if j:
                rejected += [r["id"] for r in conn.execute(
                    "update assignment set status = 'rejected', decided_by = %s, decided_at = now()"
                    " where entity_id = any(%s) and dimension_id = %s and value = %s and status = 'proposed'"
                    " and source_kind = 'model' returning id", (ref, scope_ids, f, j["value"]))]
            continue
        new, sup = _write(conn, scope_ids, f, v, ref)
        written += new
        superseded += sup
        if f in sides:
            b, side_of = sides[f]
            if side_of.get(v):
                new, sup = _write(conn, scope_ids, b, side_of[v], ref)
                written += new
                superseded += sup
    cells = []
    for f, v in clean:
        j = jev[f]
        if not j:
            continue
        level = sureness.level_of("model", j["confidence"])
        conn.execute("insert into studio_verdict (decision_id, field, value, level, side, status, confidence, agreed,"
                     " chosen, sender) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                     (did, f, j["value"], level, side, j["status"], j["confidence"], v == j["value"], v, sender))
        if j["status"] == "proposed":
            cells.append((f, j["value"], level, side))
    conn.execute("update studio_decision set settled = %s, written_ids = %s, superseded_ids = %s, rejected_ids = %s"
                 " where id = %s", (settled if decided_fields else 0, written, superseded, rejected, did))
    lifts = [lift_cell(conn, *c) for c in dict.fromkeys(cells)]
    return {"decision": did, "settled": settled if decided_fields else 0, "messages": len(scope_ids),
            "lifts": [x for x in lifts if x]}


def replan(conn: psycopg.Connection, decision_id: int) -> dict | None:
    """The planned Talos/ folders (talos.structure) follow a decision at once: its messages are
    placed again. Call it after the decision is committed: the planner rolls back on a failure,
    and must never take the owner's decision with it. Never raises."""
    d = conn.execute("select message_ids from studio_decision where id = %s and scope <> 'skip'",
                     (decision_id,)).fetchone()
    return structure.refresh_messages(conn, d["message_ids"]) if d else None


def skip(conn: psycopg.Connection, key: str, *, rep_id: int | None = None, card_kind: str = "lever") -> dict:
    """Not now: the group is not shown again for SKIP_DAYS days."""
    ids = group_ids(conn, key)
    did = conn.execute("insert into studio_decision (card, group_key, scope, rep_id) values (%s, %s, 'skip', %s)"
                       " returning id", (card_kind if card_kind in ("lever", "check") else "lever", key,
                                         rep_id if rep_id in ids else (ids[0] if ids else 0))).fetchone()["id"]
    return {"decision": did, "skipped": True}


def undo(conn: psycopg.Connection, decision_id: int | None = None) -> dict:
    """Take a decision back (the latest, by default): the rows written by it go, the human rows it
    superseded and the proposals it rejected come back. Lifts it led to stay (undo them on their own)."""
    q = ("select * from studio_decision where undone_at is null" +
         (" and id = %s" if decision_id else "") + " order by id desc limit 1")
    d = conn.execute(q, (decision_id,) if decision_id else ()).fetchone()
    if not d:
        raise StudioError("nothing to undo")
    removed = conn.execute("update assignment set status = 'superseded', decided_by = %s, decided_at = now()"
                           " where id = any(%s) and status = 'active'",
                           (f"undone:studio:{d['id']}", d["written_ids"])).rowcount
    restored = conn.execute("update assignment set status = 'active', decided_by = %s"
                            " where id = any(%s) and status = 'superseded'", (personal.OWNER_ID, d["superseded_ids"])).rowcount
    reopened = conn.execute("update assignment set status = 'proposed', decided_by = null, decided_at = null"
                            " where id = any(%s) and status = 'rejected'", (d["rejected_ids"],)).rowcount
    conn.execute("update studio_decision set undone_at = now() where id = %s", (d["id"],))
    return {"decision": d["id"], "group": d["group_key"], "scope": d["scope"], "removed": removed,
            "restored": restored, "reopened": reopened, "settled": d["settled"]}


# ---------------------------------------------------------------- lifting

def _cell_rows_sql(since: bool = False) -> str:
    """The cell's open proposals: Jev's value in the cell's level and side, on a message whose
    field has no value that counts (its own or its thread's). since: only those made since the
    cell's last lift."""
    return ("select a.id, a.entity_id, a.confidence from assignment a join message m on m.id = a.entity_id"
            " where a.dimension_id = %(f)s and a.value = %(v)s and a.status = 'proposed' and a.source_kind = 'model'"
            + (" and a.created_at >= %(since)s" if since else "") +
            f" and {sureness.level_sql(chr(39) + 'model' + chr(39), 'a.confidence')} = %(level)s"
            f" and ({search.machine_sql('m')}) = %(machine)s"
            f" and not exists (select 1 from assignment b where b.entity_id in (m.id, m.thread_id)"
            f"                 and b.dimension_id = %(f)s and b.status = 'active')")


def lift_cell(conn: psycopg.Connection, field: str, value: str, level: str, side: str, *,
              since: datetime | None = None) -> dict | None:
    """Lift a cell when the owner's verdicts show Jev is right there often enough: its open proposals are
    accepted with the calibrated confidence. None when the cell does not qualify (or holds nothing new).
    since limits it to proposals made after that time (relift: new mail only)."""
    s = cell_stats(conn).get((field, value, level, side))
    if not s or s["n"] < LIFT_MIN:
        return None
    cal = wilson_lower(s["agreed"], s["n"])
    if cal < LIFT_FLOOR - 1e-9:
        return None
    p = {"f": field, "v": value, "level": level, "machine": side == "machine", "since": since}
    rows = conn.execute(_cell_rows_sql(since is not None), p).fetchall()
    if not rows:
        return None
    lid = conn.execute("insert into studio_lift (field, value, level, side, verdicts, agreed, calibrated)"
                       " values (%s, %s, %s, %s, %s, %s, %s) returning id",
                       (field, value, level, side, s["n"], s["agreed"], round(cal, 4))).fetchone()["id"]
    ids = [r["id"] for r in conn.execute(
        "update assignment set status = 'active', decided_by = %s, decided_at = now(), confidence = %s,"
        " evidence = evidence || jsonb_build_object('jev_confidence', confidence, 'calibrated', %s::real,"
        " 'studio_lift', %s::bigint) where id = any(%s) and status = 'proposed' returning id",
        (f"studio-lift:{lid}", round(cal, 4), round(cal, 4), lid, [r["id"] for r in rows]))]
    messages = len({r["entity_id"] for r in rows})
    conn.execute("update studio_lift set assignment_ids = %s, messages = %s where id = %s", (ids, messages, lid))
    return {"lift": lid, "field": field, "value": value, "level": level, "side": side, "messages": messages,
            "calibrated": round(cal, 4), "verdicts": s["n"], "agreed": s["agreed"]}


def undo_lift(conn: psycopg.Connection, lift_id: int) -> dict:
    """Return a lift's rows to proposals, with Jev's own confidence."""
    lf = conn.execute("select * from studio_lift where id = %s and undone_at is null", (lift_id,)).fetchone()
    if not lf:
        raise StudioError(f"no lift {lift_id} to undo")
    n = conn.execute("update assignment set status = 'proposed', decided_by = null, decided_at = null,"
                     " confidence = coalesce((evidence->>'jev_confidence')::real, confidence),"
                     " evidence = evidence - 'jev_confidence' - 'calibrated' - 'studio_lift'"
                     " where id = any(%s) and status = 'active' and decided_by = %s",
                     (lf["assignment_ids"], f"studio-lift:{lift_id}")).rowcount
    conn.execute("update studio_lift set undone_at = now() where id = %s", (lift_id,))
    return {"lift": lift_id, "returned": n}


def relift(conn: psycopg.Connection) -> dict:
    """Lifted cells take the proposals that came since (new mail), while their verdicts still hold.
    Called after each sync, so new mail gets the treatment the archive got."""
    out = []
    last = {(r["field"], r["value"], r["level"], r["side"]): r["at"] for r in conn.execute(
        "select field, value, level, side, max(at) as at from studio_lift group by 1, 2, 3, 4")}
    for c in _lifted(conn):
        r = lift_cell(conn, *c, since=last.get(c))
        if r:
            out.append(r)
    return {"lifts": out, "messages": sum(r["messages"] for r in out)}


# ---------------------------------------------------------------- the counter

def stats(conn: psycopg.Connection, *, since: datetime | None = None) -> dict:
    """What the owner's decisions did: in this session (since), today, and in all."""
    def one(after):
        w = "undone_at is null" + (" and at >= %(t)s" if after else "")
        d = conn.execute(f"select count(*) filter (where scope <> 'skip')::int as decisions,"
                         f" coalesce(sum(settled), 0)::int as settled from studio_decision where {w}", {"t": after}).fetchone()
        lf = conn.execute(f"select count(*)::int as lifts, coalesce(sum(messages), 0)::int as lifted"
                          f" from studio_lift where {w}", {"t": after}).fetchone()
        return {**d, **lf, "total": d["settled"] + lf["lifted"]}
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    recent = conn.execute(
        "select id, at, card, group_key, scope, settled, array_length(message_ids, 1) as messages, lines, rep_id,"
        " (select subject from message where id = rep_id) as subject from studio_decision"
        " where undone_at is null order by id desc limit 12").fetchall()
    lifts = conn.execute("select id, at, field, value, level, side, verdicts, agreed, calibrated, messages"
                         " from studio_lift where undone_at is null order by id desc limit 20").fetchall()
    return {"session": one(since) if since else None, "today": one(midnight), "all": one(None),
            "recent": recent, "lifts": lifts}


def calibration(conn: psycopg.Connection) -> list[dict]:
    """Per cell the owner has judged: how often Jev was right there, and how far it is from a lift."""
    lifted = _lifted(conn)
    out = []
    for r in conn.execute(
            "select v.field, v.value, v.level, v.side, v.status, count(*)::int as n,"
            " count(*) filter (where v.agreed)::int as a from studio_verdict v"
            " join studio_decision d on d.id = v.decision_id where d.undone_at is null group by 1, 2, 3, 4, 5"
            " order by 6 desc"):
        k = (r["field"], r["value"], r["level"], r["side"])
        out.append({**r, "rate": round(r["a"] / r["n"], 3), "lower": round(wilson_lower(r["a"], r["n"]), 3),
                    "need": need(r["a"], r["n"]) if r["status"] == "proposed" else None, "lifted": k in lifted})
    return out


def undecided(conn: psycopg.Connection) -> dict:
    """Totals for Today: how much mail still has an unsettled field, from the cached pool."""
    row = insight.read(conn, POOL)
    if not row or not row["payload"]:
        return {"known": False}
    p = row["payload"]
    return {"known": True, "groups": len(p.get("groups") or []),
            "top": sum(g["messages"] for g in (p.get("groups") or [])[:10]),
            "totals": p.get("totals") or {}}


def parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        raise StudioError("since is an ISO time") from None
