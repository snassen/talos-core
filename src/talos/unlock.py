"""Unlocking the labelling: the low-hanging fruit, and the improvement jobs (Tune › Jobs & fruit).

**Low-hanging fruit.** Most undecided mail comes in big sender groups: one system sending the same
kind of mail for years. One answer for a group's representative, given to the whole group, decides
all of it. `fruit()` ranks those groups by how many messages that would unlock.

- *Undecided* (per incoming email message, its own values and its thread's, any source, only
  active ones): no kind, no topic or no value; or no sender_kind (an origin counts, since
  sender_kind is its side); or no keep (a value counts, since keep is its side). The accepted
  levels are the ones accept applied: a proposal under its level is not active, so it is missing.
- *People's* mail is left out (sender_kind people, or a people origin): one answer for a
  colleague's or a friend's mail is no answer. Mail with no sender side yet stays in.
- *Groups* are gold's sender groups (gold.uncertain_units): one sender, split by system for a
  shared sender (a robot address by its [tag], a trailing status word, or the first word), five or more
  messages. A message already in any answer key (an item, or a sender group's cases or messages)
  is left out, so a group the owner has in a set does not come back; it still counts when a shared
  sender's systems are told apart, so the split is the same before and after.
- Per group: the messages it would unlock, which fields are missing (and on how many), two example
  subjects, and a representative: the newest message of its biggest subject pattern.

The ranking is computed once and kept in insight_cache (about 3 s on the real archive); the
page reads it, and computes it again behind the page when its fingerprint moves (new mail,
a write to assignment, a new answer key). `talos sync --then-rules` and `talos enrich accept`
refresh it at once, and making an unlock set stores what is left.

`label_set()` draws the top groups again, fresh, and makes them a small answer key (params.kind
'unlock', label_fields the answer-key fields for what is missing: sender_kind is asked as origin,
keep as value), each item a sender group whose messages are frozen in gold_group_message. The owner's
answers are given to the groups by `apply()`: gold.propagate_groups, plus the sides of those answers (sender_kind from the origin, keep
from the value), written as the owner's too, so the structure plan can place the mail. A dry run first.

`export_for_claude()` writes the set's items in the blind format (gold.item, one JSON line each)
and the fields' options to files, and says the exact command that imports labels later. It calls
no model.

**Improvement jobs** (`jobs()`): per field, a focused Jev run over the cases Jev was unsure of
(talos.focus `--where uncertain`), with the messages it could improve, the undecided messages and
the proposals just under the accept level (NEAR_LOW to the level), the confusions measured on the
answer key (the default answer-key run, as the acceptance explorer reads it), and the cost from the
tokens per case measured by earlier focused runs of that field (else estimated on a sample of its
records, as the focus dry run estimates). Ranked by messages. Each carries its commands, the dry
run first; the UI runs nothing. Done and dismissed jobs are kept in improvement_job and not shown.
The jobs take about 35 s on the real archive (kind's uncertain cases alone 20 s), so they are only
computed behind the page, when a run or an accept or the answer key changed.

The jobs overlap: on one archive the eight asked 160,931 cases between them, but only 64,773
distinct ones (a message unsure of kind is often unsure of topic too). So a further job, *all Jev
jobs in one run* (focus.run_many), asks each of those cases once with only the fields it is unsure
of: 2.3 times fewer requests, the record sent once, and about a quarter fewer tokens.
"""

from __future__ import annotations

import json
import random
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from talos import acceptance, backfill, boundary, focus, gold, insight, jev, jev_gold

KEY_FIELDS = ("kind", "topic", "value", "sender_kind", "keep")
# What decides each key field: the field itself, or the field its side is derived from.
DECIDED_BY = {"kind": ("kind",), "topic": ("topic",), "value": ("value",), "sender_kind": ("sender_kind", "origin"),
              "keep": ("keep", "value")}
# The answer-key field that answers a missing key field.
GOLD_FIELD = {"kind": "kind", "topic": "topic", "value": "value", "sender_kind": "origin", "keep": "value"}
LABEL_ORDER = ("origin", "kind", "type", "topic", "ask", "value", "route")
PERSON_ORIGINS = ("person", "person_via_system", "list")
TOP = 10
KEEP = 25                  # groups kept in the cache (the widget shows TOP)
SAMPLER = "unlock-1"
FRUIT = "unlock-fruit"
JOBS = "unlock-jobs"


class UnlockError(ValueError):
    pass


# ---------------------------------------------------------------- the low-hanging fruit

_UNDECIDED_SQL = """
with base as materialized (
    select m.id, m.thread_id, m.account_id, lower(m.from_address) as sender, m.from_name, m.subject, m.received_at
    from message m where m.medium = 'email' and m.direction = 'in' and m.from_address is not null),
c as (
    select b.id as mid, a.dimension_id as dim, a.value,
           array[case a.source_kind when 'human' then 4 when 'rule' then 3 when 'model' then 2 else 1 end,
                 (a.entity_id = b.id)::int, extract(epoch from a.created_at)::bigint, a.id] as rank
    from base b join assignment a on a.entity_id = any(array[b.id, b.thread_id]) and a.status = 'active'
                                 and a.dimension_id = any(%(dims)s)),
v as (select mid, array_agg(distinct dim) as dims,
             (array_agg(value order by rank desc) filter (where dim = 'sender_kind'))[1] as sender_kind,
             (array_agg(value order by rank desc) filter (where dim = 'origin'))[1] as origin
      from c group by mid),
held as (select message_id as id from gold_item
         union select unnest(c.member_ids) from gold_group_case g join enrich_case c on c.id = g.case_id
         union select message_id from gold_group_message)
select b.id, b.account_id, b.sender, b.from_name, b.subject, b.received_at, mp.pattern_key,
       coalesce(v.dims, '{}') as dims, exists (select 1 from held where held.id = b.id) as held
from base b left join v on v.mid = b.id left join message_pattern mp on mp.message_id = b.id
where coalesce(v.sender_kind, case when v.origin = any(%(people)s) then 'people' when v.origin is not null
                                   then 'machine' end, 'machine') <> 'people'"""


def _missing(dims) -> list[str]:
    have = set(dims or ())
    return [f for f in KEY_FIELDS if not have.intersection(DECIDED_BY[f])]


def groups(conn: psycopg.Connection, *, group_min: int = gold.GROUP_MIN) -> dict:
    """Every sender group of undecided machine-side mail, biggest first, with its message ids. Plain
    SELECTs: works read-only."""
    t0 = time.monotonic()
    rows = conn.execute(_UNDECIDED_SQL, {"dims": sorted({d for v in DECIDED_BY.values() for d in v}),
                                         "people": list(PERSON_ORIGINS)}).fetchall()
    t_sql = time.monotonic() - t0
    for r in rows:
        r["open"] = not r["held"] and bool(_missing(r["dims"]))
    by_id = {r["id"]: r for r in rows}
    cases = [{"id": r["id"], "stage": "machine", "sender": r["sender"], "subject": r["subject"],
              "member_ids": [r["id"]]} for r in rows]
    # Grouped with the sender's decided mail and the mail already in a set, so a shared sender
    # splits by system the same way before and after one of its systems was labelled; only the
    # undecided mail that is in no set is then counted.
    units = gold.uncertain_units(cases, group_min=group_min)
    out = []
    for g in units["group"]:
        ids = [i for i in g["cases"] if by_id[i]["open"]]
        if len(ids) < group_min:
            continue
        msgs = [by_id[i] for i in ids]
        missing = Counter(f for m in msgs for f in _missing(m["dims"]))
        pats = Counter(m["pattern_key"] or f"m{m['id']}" for m in msgs)
        newest: dict[str, dict] = {}
        for m in msgs:
            k = m["pattern_key"] or f"m{m['id']}"
            cur = newest.get(k)
            if cur is None or (m["received_at"], m["id"]) > (cur["received_at"], cur["id"]):
                newest[k] = m
        # the biggest subject patterns first; among equals, the one with the newest message
        top_pats = sorted(pats, key=lambda k: (-pats[k], -newest[k]["received_at"].timestamp()
                                               if newest[k]["received_at"] else 0, -newest[k]["id"]))
        rep = newest[top_pats[0]]
        names = Counter(m["from_name"] for m in msgs if m["from_name"])
        out.append({"key": g["sender"] + (f" | {g['system']}" if g["system"] else ""), "sender": g["sender"],
                    "system": g["system"], "name": names.most_common(1)[0][0] if names else None,
                    "account": Counter(m["account_id"] for m in msgs).most_common(1)[0][0],
                    "messages": len(ids), "missing": {f: missing[f] for f in KEY_FIELDS if missing[f]},
                    "fields": _gold_fields(missing),
                    "examples": [{"subject": newest[k]["subject"], "received_at": newest[k]["received_at"],
                                  "messages": pats[k]} for k in top_pats[:2]],
                    "rep": rep["id"], "pattern_key": rep["pattern_key"], "ids": sorted(ids)})
    out.sort(key=lambda g: (-g["messages"], g["key"]))
    return {"groups": out, "undecided": sum(1 for r in rows if r["open"]), "grouped": sum(g["messages"] for g in out),
            "sql_seconds": round(t_sql, 2), "seconds": round(time.monotonic() - t0, 2)}


def _gold_fields(missing) -> list[str]:
    want = {GOLD_FIELD[f] for f in missing}
    return [f for f in LABEL_ORDER if f in want]


def fruit_fingerprint(conn: psycopg.Connection) -> str:
    r = conn.execute("select (select coalesce(max(id), 0) from message) as m, (select coalesce(max(id), 0) from assignment) as a,"
                     " (select count(*) from gold_item) as gi, (select count(*) from gold_group_message) as gm,"
                     " (select count(*) from gold_group_case) as gc").fetchone()
    return insight.fingerprint([r, insight.table_writes(conn, ["assignment"])])


def compute_fruit(conn: psycopg.Connection) -> dict:
    return _payload(groups(conn))


def _payload(g: dict) -> dict:
    top = [{k: v for k, v in x.items() if k != "ids"} for x in g["groups"][:KEEP]]
    return {"top": top, "undecided": g["undecided"], "groups": len(g["groups"]), "grouped": g["grouped"],
            "top_messages": sum(x["messages"] for x in top[:TOP]), "sql_seconds": g["sql_seconds"]}


def fruit(conn: psycopg.Connection, *, dsn: str | None = None, refresh: bool = False) -> dict:
    """The widget: the cached ranking (TOP of it), and the unlock sets and where each stands."""
    row = insight.get(conn, FRUIT, fruit_fingerprint, compute_fruit, dsn=dsn, refresh=refresh)
    p = row["payload"]
    return {**p, "top": p["top"][:TOP], "computed_at": row["computed_at"], "seconds": row["seconds"],
            "stale": row["stale"], "refreshing": row["refreshing"], "sets": sets(conn)}


def refresh_fruit(conn: psycopg.Connection, *, if_stale: bool = True) -> dict | None:
    """After a sync or an accept: compute the ranking again, when what it was computed from has
    moved (or always, if_stale=False). Never raises."""
    try:
        row = insight.read(conn, FRUIT)
        if if_stale and row and row["fingerprint"] == fruit_fingerprint(conn):
            return row
        return insight.compute(conn, FRUIT, fruit_fingerprint, compute_fruit)
    except Exception:
        conn.rollback()
        insight.log.exception("unlock: refreshing the low-hanging fruit failed")
        return None


def sets(conn: psycopg.Connection) -> list[dict]:
    """The unlock answer keys, newest first: how far the owner (and Claude) labelled each, and whether its
    answers were given to the groups."""
    out = []
    for r in conn.execute("select s.id, s.name, s.params, s.created_at, count(i.id)::int as items from gold_set s"
                          " join gold_item i on i.set_id = s.id where s.params->>'kind' = 'unlock'"
                          " and not coalesce((s.params->>'archived')::boolean, false)"
                          " group by s.id order by s.id desc limit 5"):
        fields = gold._fields_of(r["params"])
        done = gold._done_count(conn, r["id"], fields)
        claude = gold._done_count(conn, r["id"], fields, "claude")
        out.append({"id": r["id"], "name": r["name"], "created_at": r["created_at"], "items": r["items"],
                    "fields": fields, "done": done, "claude_done": claude,
                    "messages": sum(g.get("messages", 0) for g in r["params"].get("groups", [])),
                    "applied": r["params"].get("applied"), "export": r["params"].get("export")})
    return out


def _item(conn: psycopg.Connection, g: dict) -> dict:
    """The answer-key item for a group: its representative with up to three other messages of the
    same subject pattern in the group as context (a pattern unit), else the message alone."""
    same = [r["id"] for r in conn.execute(
        "select m.id from message m join message_pattern mp on mp.message_id = m.id"
        " where mp.pattern_key = %s and m.id = any(%s) and m.id <> %s order by m.received_at desc, m.id desc limit 3",
        (g["pattern_key"], g["ids"], g["rep"]))] if g["pattern_key"] else []
    thread = conn.execute("select thread_id from message where id = %s", (g["rep"],)).fetchone()["thread_id"]
    miss = ", ".join(f"{f} {n}" for f, n in g["missing"].items())
    return {"stratum": "machine", "message_id": g["rep"], "thread_id": thread,
            "unit": "pattern" if same else "message", "pattern_key": g["pattern_key"] if same else None,
            "context_ids": same,
            "reason": f"unlock · sender group {g['key']}: {g['messages']} undecided messages (missing {miss})",
            "info": {"group": "sender", "sampler": SAMPLER,
                     "sender_group": {"key": g["key"], "sender": g["sender"], "system": g["system"], "stage": "machine",
                                      "cases": g["messages"], "messages": g["messages"]},
                     "missing": g["missing"]}}


def label_set(conn: psycopg.Connection, *, top: int = TOP, name: str | None = None) -> dict:
    """A small answer key of the top groups' representatives, drawn fresh (so nothing already in a
    set comes in). Its fields: the answer-key fields for what the groups miss."""
    if top < 1:
        raise UnlockError("top is at least 1")
    t0 = time.monotonic()
    every = groups(conn)
    g = every["groups"][:top]
    if not g:
        raise UnlockError("no sender group of undecided mail is left to label")
    missing: Counter = Counter()
    for x in g:
        missing.update(x["missing"])
    fields = _gold_fields(missing)
    used = {r["seed"] for r in conn.execute("select seed from gold_set")}
    seed = None
    while seed is None or seed in used:
        seed = random.SystemRandom().randrange(1, 10**6)
    items = [_item(conn, x) for x in g]
    total = sum(x["messages"] for x in g)
    with conn.transaction():
        sid = conn.execute(
            "insert into gold_set (name, seed, target, params) values (%s, %s, %s, %s) returning id",
            (name or f"Unlock {datetime.now(timezone.utc):%Y-%m-%d}: top {len(g)} sender groups, {total:,} messages",
             seed, len(g),
             Jsonb({"kind": "unlock", "sampler": SAMPLER, "label_fields": fields, "missing": dict(missing),
                    "groups": [{"key": x["key"], "messages": x["messages"], "missing": x["missing"]} for x in g]}))
        ).fetchone()["id"]
        for pos, (it, x) in enumerate(zip(items, g), 1):
            iid = conn.execute(
                "insert into gold_item (set_id, position, message_id, stratum, reason, unit, thread_id, pattern_key,"
                " context_ids, window_first_id, window_last_id, info)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, null, null, %s) returning id",
                (sid, pos, it["message_id"], it["stratum"], it["reason"], it["unit"], it["thread_id"], it["pattern_key"],
                 it["context_ids"], Jsonb(it["info"]))).fetchone()["id"]
            with conn.cursor() as cur:
                cur.executemany("insert into gold_group_message (item_id, message_id) values (%s, %s)",
                                [(iid, m) for m in x["ids"]])
    # What is left is the fruit now (the set holds these groups' mail): stored at once, no second pass.
    rest = every["groups"][top:]
    insight.store(conn, FRUIT, fruit_fingerprint(conn),
                  _payload({**every, "groups": rest, "undecided": every["undecided"] - total,
                            "grouped": every["grouped"] - total}), round(time.monotonic() - t0, 2))
    return {"set_id": sid, "items": len(items), "fields": fields, "messages": total, "seed": seed,
            "groups": [{"key": x["key"], "messages": x["messages"]} for x in g]}


def _labeller_for(conn: psycopg.Connection, set_id: int, labeller: str | None) -> str:
    fields = gold.set_fields(conn, set_id)
    n = conn.execute("select count(*)::int as n from gold_item where set_id = %s", (set_id,)).fetchone()["n"]
    if labeller:
        who = gold._labeller(labeller)
        if gold._done_count(conn, set_id, fields, who) < n:
            raise UnlockError(f"answer key {set_id} is not fully labelled by {who} yet")
        return who
    for who in (gold.OWNER, "claude"):
        if gold._done_count(conn, set_id, fields, who) >= n:
            return who
    raise UnlockError(f"answer key {set_id} is not fully labelled yet: every item needs every one of its fields")


def apply(conn: psycopg.Connection, set_id: int, *, labeller: str | None = None, dry_run: bool = True) -> dict:
    """Give the answers of a fully labelled unlock set to its groups: gold.propagate_groups, and the
    sides of the answers (sender_kind from origin, keep from value), written as the labeller's
    decisions too (source_ref 'gold:<set>:<item>:side'). A dry run (the default) counts and rolls back."""
    params = (conn.execute("select params from gold_set where id = %s", (set_id,)).fetchone() or {}).get("params")
    if params is None:
        raise UnlockError(f"no answer key {set_id}")
    if params.get("kind") != "unlock":
        raise UnlockError(f"answer key {set_id} is not an unlock set")
    who = _labeller_for(conn, set_id, labeller)
    out: dict = {}
    with conn.transaction():
        res = gold.propagate_groups(conn, set_id, labeller=who, dry_run=False)
        sides = _write_sides(conn, set_id, who)
        res.update(dry_run=dry_run, sides=sides, sides_written=sum(sides.values()))
        res["messages"] = sum(i["messages"] for i in res["items"])
        if not dry_run:
            conn.execute("update gold_set set params = params || jsonb_build_object('applied', %s::jsonb) where id = %s",
                         (Jsonb({"at": datetime.now(timezone.utc).isoformat(), "labeller": who,
                                 "written": res["written"] + res["sides_written"]}), set_id))
        out = res
        if dry_run:
            raise psycopg.Rollback()
    return out


def _write_sides(conn: psycopg.Connection, set_id: int, labeller: str) -> dict:
    """sender_kind and keep for each group's messages, from the labeller's sure origin and value."""
    try:
        sd = boundary.sides(conn)
    except boundary.BoundaryError:
        return {}
    written: Counter = Counter()
    items = conn.execute("select id, position, info from gold_item where set_id = %s and info ? 'sender_group'"
                         " order by position", (set_id,)).fetchall()
    labels = gold._labels(conn, [i["id"] for i in items], labeller)
    for it in items:
        lab = labels.get(it["id"], {})
        kind, prefix = gold.source_for(labeller)
        ref = f"{prefix}:{set_id}:{it['position']}:side"
        want = []
        if gold.MIXED not in lab:
            for b in ("sender_kind", "keep"):
                src = boundary.SOURCE[b]
                ans = lab.get(src) or {}
                if ans.get("status") == "set" and ans.get("values") and sd[b]["side_of"].get(ans["values"][0]):
                    want.append((b, sd[b]["side_of"][ans["values"][0]], src))
        conn.execute("update assignment a set status = 'superseded', decided_at = now()"
                     " where a.source_kind = %s and a.source_ref = %s and a.status = 'active'"
                     " and not exists (select 1 from unnest(%s::text[], %s::text[]) w(f, v)"
                     "                 where w.f = a.dimension_id and w.v = a.value)",
                     (kind, ref, [b for b, _, _ in want], [v for _, v, _ in want]))
        msgs = gold._group_messages(conn, it["id"])
        for b, side, src in want:
            ev = Jsonb({"propagated_from": {"set": set_id, "item": it["position"], "item_id": it["id"]},
                        "side_of": src, "sender_group": it["info"]["sender_group"].get("key"), "labeller": labeller})
            written[b] += conn.execute(
                "insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, status, evidence,"
                " decided_by, decided_at)"
                " select m.id, %s, %s, %s, %s, 'active', %s, %s, now() from unnest(%s::bigint[]) m(id)"
                " where not exists (select 1 from assignment a where a.entity_id = m.id and a.dimension_id = %s"
                "                   and a.status = 'active'"
                "                   and ((a.source_kind = 'human' and coalesce(a.source_ref, '') <> %s)"
                "                        or (a.source_ref = %s and a.value = %s)))",
                (b, side, kind, ref, ev, labeller, msgs, b, ref, ref, side)).rowcount
    return dict(written)


def export_for_claude(conn: psycopg.Connection, set_id: int, out_dir: Path) -> dict:
    """The set's items in the blind format (gold.item: the message, its context and the group's
    size and subjects; never a value Talos decided), one JSON line per item, and the options of its
    fields, as files. Returns their paths and the commands that import labels later. No model is called."""
    row = conn.execute("select params from gold_set where id = %s", (set_id,)).fetchone()
    if not row:
        raise UnlockError(f"no answer key {set_id}")
    fields = gold.set_fields(conn, set_id)
    positions = [r["position"] for r in conn.execute(
        "select position from gold_item where set_id = %s order by position", (set_id,))]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    items_path = out_dir / f"unlock-set-{set_id}-blind-{stamp}.jsonl"
    options_path = out_dir / f"unlock-set-{set_id}-options-{stamp}.json"
    labels_path = out_dir / f"unlock-set-{set_id}-labels-claude.jsonl"
    with items_path.open("w", encoding="utf-8") as fh:
        for pos in positions:
            it = gold.item(conn, set_id, pos)
            for k in ("labels", "duration_ms", "round_items", "revealed", "round"):
                it.pop(k, None)
            fh.write(json.dumps(it, ensure_ascii=False, default=str) + "\n")
    opts = gold.options(conn)
    options_path.write_text(json.dumps(
        {"set_id": set_id, "fields": fields, "many": [f for f in fields if f in gold.MANY_FIELDS],
         "options": {f: [{k: o[k] for k in ("value", "label", "family", "description")} for o in opts[f]]
                     for f in fields},
         "answer_line": _answer_example(fields),
         "rules": "One JSON line per item: its position, every field of the set (a value from the options; "
                  "ask and route a list, empty for none), 'unsure': [fields you are not sure of], optional 'note'. "
                  "A sender-group item stands for all the messages of its group: answer for the group."},
        ensure_ascii=False, indent=2), encoding="utf-8")
    cmds = {"import": f"uv run talos enrich gold import-labels --set {set_id} --labeller claude {labels_path}",
            "dry_run": f"uv run talos enrich gold propagate-groups --set {set_id} --labeller claude",
            "apply": "Tune › Jobs & fruit › Low-hanging fruit › Apply to the groups (a dry run first), or: "
                     f"uv run talos enrich gold propagate-groups --set {set_id} --labeller claude --apply"}
    info = {"items": str(items_path), "options": str(options_path), "labels": str(labels_path),
            "commands": cmds, "at": stamp}
    conn.execute("update gold_set set params = params || jsonb_build_object('export', %s::jsonb) where id = %s",
                 (Jsonb(info), set_id))
    return {"set_id": set_id, "count": len(positions), "fields": fields, **info}


def _answer_example(fields: list[str]) -> dict:
    ex = {"position": 1}
    for f in fields:
        ex[f] = [] if f in gold.MANY_FIELDS else f"<one {f} value>"
    return {**ex, "unsure": [], "note": ""}


# ---------------------------------------------------------------- improvement jobs

JOB_FIELDS = ("kind", "topic", "value", "origin", "route", "ask", "sender_kind", "keep")
NEAR_LOW = 0.60
ESTIMATE_SAMPLE = 40
CONFUSIONS = 6


def _threshold(field: str) -> float:
    return focus.THRESHOLDS[field]


def jobs_fingerprint(conn: psycopg.Connection) -> str:
    runs = conn.execute("select string_agg(id || ':' || coalesce(params->'accept'->>'at', ''), ',' order by id) as s"
                        " from model_run where purpose = any(%s) or purpose = %s",
                        (list(backfill.PURPOSES), jev_gold.PURPOSE)).fetchone()["s"]
    labels = conn.execute("select count(*) as n from gold_label").fetchone()["n"]
    return insight.fingerprint([runs, labels])


def _proposals(conn: psycopg.Connection, runs: list[str]) -> dict:
    """Per field: messages with a proposal of the latest runs and no active value (undecided), and
    those of them whose proposal is just under the accept level (near)."""
    fields = list(JOB_FIELDS)
    ts = [_threshold(f) for f in fields]
    rows = conn.execute(
        "select a.dimension_id as f, count(distinct a.entity_id)::int as undecided,"
        " count(distinct a.entity_id) filter (where a.confidence >= %(low)s and a.confidence < t.t)::int as near"
        " from assignment a join unnest(%(f)s::text[], %(t)s::float[]) t(f, t) on t.f = a.dimension_id"
        " where a.source_kind = 'model' and a.status = 'proposed' and a.source_ref = any(%(runs)s)"
        " and not exists (select 1 from assignment b where b.entity_id = a.entity_id"
        "                 and b.dimension_id = a.dimension_id and b.status = 'active')"
        " group by 1", {"low": NEAR_LOW, "f": fields, "t": ts, "runs": runs}).fetchall()
    return {r["f"]: r for r in rows}


def _uncertain(conn: psycopg.Connection, field: str) -> dict:
    """The cases a focused run --where uncertain would ask, and the messages they stand for. keep is
    not asked alone (it is value's side): its uncertain cases are value's cases whose keep side is
    under its level, and its run is a value run."""
    if field == "keep":
        stages = [s for s in backfill.STAGES]
        chain = [r["id"] for r in focus._chain(conn, stages)]
        latest = focus._latest(conn, "keep", chain, stages)
        hit = [r for r in latest if focus._uncertain(r, "keep")]
        units = {(r["stage"], r["unit_key"]) for r in hit}
        todo = focus._cases(conn, [r for r in latest if (r["stage"], r["unit_key"]) in units])
        return {"cases": todo, "units": len(units)}
    sel = focus.select(conn, field, "uncertain")
    return {"cases": sel["cases"], "units": sel["units"]}


def _tokens(conn: psycopg.Connection, field: str, todo: list) -> tuple[int, str]:
    """Tokens per case: measured on earlier focused runs of the field, else estimated on a sample."""
    asked = "value" if field == "keep" else field
    r = conn.execute("select avg(c.input_tokens)::float as t, count(*)::int as n from enrich_case c"
                     " join model_run r on r.id = c.run_id where r.purpose = %s and r.params->'focus'->>'field' = %s"
                     " and c.error is null and c.input_tokens > 0", (backfill.FOCUS_PURPOSE, asked)).fetchone()
    if r["n"]:
        return round(r["t"]), f"measured on {r['n']:,} cases of earlier focused {asked} runs"
    if not todo:
        return 0, "no cases"
    qsets = focus.question_sets(focus.dims_for(conn, asked), asked)
    step = max(1, len(todo) // ESTIMATE_SAMPLE)
    est = focus.estimate(conn, todo[::step][:ESTIMATE_SAMPLE], qsets, jev.MODEL)
    return est["input_tokens_per_case"], f"estimated on {est['sampled']} of its records"


def _confusions(conn: psycopg.Connection) -> dict:
    """Per field, on the answer key (the acceptance explorer's default run): Jev's accuracy, the
    top confusions ('Jev said → the owner's answer') and the values that are confused with each other."""
    run = acceptance.default_gold_run(conn)
    if not run:
        return {}
    try:
        sd = boundary.sides(conn)
        cases = acceptance.gold_cases(conn, run, sd)
    except (boundary.BoundaryError, ValueError):
        return {}
    labels = acceptance._labels(conn)
    out = {"_run": run}
    for f, rows in cases["fields"].items():
        sc = acceptance.score(rows, 0.0)
        wrong: Counter = Counter(f"{x['p']}→{x['r']}" for x in rows if x["p"] != x["r"])
        name = labels.get(f, {}).get("values", {})
        pairs = [{"said": k.split("→")[0], "owner": k.split("→")[1], "items": n} for k, n in wrong.most_common(CONFUSIONS)]
        for p in pairs:
            p["said_label"], p["owner_label"] = name.get(p["said"], p["said"]), name.get(p["owner"], p["owner"])
        # values confused with each other: the connected groups of the top pairs
        parent: dict[str, str] = {}
        for p in pairs:
            parent[_root(parent, p["said"])] = _root(parent, p["owner"])
        comp: dict[str, list] = defaultdict(list)
        for v in list(parent):
            comp[_root(parent, v)].append(v)
        groups_ = []
        for vs in comp.values():
            n = sum(p["items"] for p in pairs if p["said"] in vs)
            groups_.append({"values": sorted(vs), "labels": [name.get(v, v) for v in sorted(vs)], "items": n})
        groups_.sort(key=lambda g: -g["items"])
        out[f] = {"items": sc["n"], "right": sc["right"], "accuracy": sc["accuracy"], "pairs": pairs,
                  "groups": groups_[:3]}
    return out


def _root(parent: dict[str, str], v: str) -> str:
    while parent.setdefault(v, v) != v:
        v = parent[v]
    return v


def compute_jobs(conn: psycopg.Connection) -> dict:
    """The improvement jobs, computed in full (about 35 s: see the module docstring). Per field: the
    cases a focused run would ask, the messages they stand for, the proposals undecided or just under
    the level, the answer key's confusions, the tokens and the cost, and the commands (the dry run
    first); then, when two or more fields have work, the same as one combined run. Ranked by messages."""
    t0 = time.monotonic()
    runs = [r["id"] for r in acceptance.archive_runs(conn)]
    if not runs:
        return {"jobs": [], "note": "No backfill run yet: talos enrich jev backfill comes first.", "timings": {}}
    props = _proposals(conn, runs)
    t_props = time.monotonic() - t0
    conf = _confusions(conn)
    timings = {"proposals": round(t_props, 2)}
    labels = {r["id"]: r["label"] for r in conn.execute("select id, label from dimension where id = any(%s)",
                                                         (list(JOB_FIELDS),))}
    out, found = [], {}
    for f in JOB_FIELDS:
        t1 = time.monotonic()
        try:
            u = _uncertain(conn, f)
        except backfill.BackfillError as exc:
            out.append({"key": f"focus:{f}:uncertain", "field": f, "error": str(exc)})
            continue
        todo = u["cases"]
        found[f] = todo
        messages = len({m for c in todo for m in c.members})
        per, how = _tokens(conn, f, todo)
        asked = "value" if f == "keep" else f
        qv = focus.question_version(focus.question_sets(focus.dims_for(conn, asked), asked))
        checked = bool(focus.gold_runs(conn, asked, qv))
        p = props.get(f) or {"undecided": 0, "near": 0}
        c = conf.get(f)
        steps = []
        if c and c["groups"] and f not in ("sender_kind", "keep"):
            vals = [v for g in c["groups"][:1] for v in g["values"]]
            steps.append({"text": f"Sharpen the definitions of {', '.join(vals)} in rules/taxonomy.json (the answer key"
                                  f" confuses them), then load them", "command": "uv run talos taxonomy load"})
        if not checked:
            steps.append({"text": "Ask the answer key first (a real run is refused until then)",
                          "command": f"uv run talos enrich jev focus --field {asked} --gold"})
        steps += [{"text": "Dry run: the cases, the tokens and the cost; nothing is sent",
                   "command": f"uv run talos enrich jev focus --field {asked} --where uncertain --dry-run"},
                  {"text": "The run", "command": f"uv run talos enrich jev focus --field {asked} --where uncertain"},
                  {"text": "Accept what now clears the levels (a dry run first)",
                   "command": "uv run talos enrich accept --run all --two-level --dry-run"}]
        tokens = per * len(todo)
        out.append({"key": f"focus:{f}:uncertain", "kind": "jev", "field": f, "label": labels.get(f, f),
                    "asked": asked, "threshold": _threshold(f), "near_band": [NEAR_LOW, _threshold(f)],
                    "messages": messages, "cases": len(todo), "units": u["units"],
                    "undecided": p["undecided"], "near": p["near"], "confusions": c,
                    "tokens_per_case": per, "tokens_how": how, "input_tokens": tokens,
                    "cost_usd": round(jev.cost(tokens), 2),
                    "minutes": round(len(todo) / backfill.MEASURED_CASES_PER_SECOND / 60, 1),
                    "gold_checked": checked, "steps": steps})
        timings[f] = round(time.monotonic() - t1, 2)
    t1 = time.monotonic()
    if sum(1 for todo in found.values() if todo) > 1:
        out.append(_combined(conn, found))
        timings["combined"] = round(time.monotonic() - t1, 2)
    out.sort(key=lambda j: (-(j.get("messages") or 0), j["field"] or ""))
    return {"jobs": out, "gold_run": conf.get("_run"), "timings": timings, "near_low": NEAR_LOW}


def _combined(conn: psycopg.Connection, found: dict[str, list]) -> dict:
    """The jobs above as one combined focused run (focus.run_many): the same cases, each unit asked
    once, with only the fields it is unsure of. The jobs overlap a lot (a message unsure of kind is
    often unsure of topic too), so it sends far fewer requests, and each record once."""
    fields = [f for f in JOB_FIELDS if found.get(f)]
    unit_fields: dict[tuple, set] = {}
    cases = {}
    for f, todo in found.items():
        a = focus.asked(f)
        for c in todo:
            unit_fields.setdefault((c.stage, c.key), set()).update({a} | ({focus.COMPANION[a]} if a in focus.COMPANION
                                                                          else set()))
            cases[(c.stage, c.key, c.sample)] = c
    order = [focus.asked(f) for f in fields] + list(focus.COMPANION.values())
    sel = {"unit_fields": {k: tuple(f for f in dict.fromkeys(order) if f in v) for k, v in unit_fields.items()}}
    qsets, _dims, qversions, per_case = focus._asking(conn, sel)
    todo = list(cases.values())
    est = focus.estimate_many(conn, todo, qsets, per_case, jev.MODEL)
    est.pop("example", None)
    unchecked = [f for f, qv in qversions.items() if not focus.gold_runs(conn, f, qv)]
    spec = ",".join(fields)
    steps = []
    if unchecked:
        steps.append({"text": f"Ask the answer key first, all at once ({', '.join(unchecked)} not yet checked)",
                      "command": f"uv run talos enrich jev focus --field {spec} --gold"})
    steps += [{"text": "Dry run: the cases per field, what they share, the tokens and the cost; nothing is sent",
               "command": f"uv run talos enrich jev focus --field {spec} --where uncertain --dry-run"},
              {"text": "The run",
               "command": f"uv run talos enrich jev focus --field {spec} --where uncertain --max-cases {len(todo)}"},
              {"text": "Accept what now clears the levels (a dry run first)",
               "command": "uv run talos enrich accept --run all --two-level --dry-run"}]
    return {"key": "focus:combined:uncertain", "kind": "jev", "combined": True, "field": None,
            "label": "All Jev jobs in one run", "fields": fields,
            "messages": len({m for c in todo for m in c.members}), "cases": len(todo),
            "separate_cases": sum(len(v) for v in found.values()), "asks": est["asks"],
            "units": len(unit_fields), "tokens_per_case": est["input_tokens_per_case"],
            "separate_tokens_per_case": est["separate_tokens_per_case"], "input_tokens": est["input_tokens"],
            "cost_usd": round(est["cost_usd"], 2), "separate_cost_usd": round(est["separate_cost_usd"], 2),
            "minutes": round(len(todo) / backfill.MEASURED_CASES_PER_SECOND / 60, 1),
            "gold_checked": not unchecked, "unchecked": unchecked, "steps": steps}


def jobs(conn: psycopg.Connection, *, dsn: str | None = None, refresh: bool = False, hidden: bool = False) -> dict:
    """The widget: the cached jobs with the Claude job from the fruit, minus done and dismissed ones."""
    row = insight.get(conn, JOBS, jobs_fingerprint, compute_jobs, dsn=dsn, refresh=refresh, first_behind=True)
    p = row["payload"] or {"jobs": [], "note": "Computing the jobs behind the page (about half a minute on the"
                                                " whole archive)…"}
    states = {r["key"]: r for r in conn.execute("select key, state, decided_at from improvement_job")}
    all_jobs = list(p.get("jobs") or [])
    fr = insight.read(conn, FRUIT)
    if fr and fr["payload"].get("top"):
        top = fr["payload"]["top"][:TOP]
        all_jobs.append({"key": "claude:unlock", "kind": "claude", "field": None, "label": "Claude labels the sender groups",
                         "messages": sum(g["messages"] for g in top), "groups": len(top),
                         "steps": [{"text": "Tune › Jobs & fruit › Low-hanging fruit › Let Claude label these: the items go to a"
                                            " file in the blind format, with the command that imports the labels"},
                                   {"text": "Then Apply to the groups (a dry run first)"}]})
        all_jobs.sort(key=lambda j: (-(j.get("messages") or 0), j["key"]))
    shown = [j for j in all_jobs if hidden or j["key"] not in states]
    for j in shown:
        j["state"] = (states.get(j["key"]) or {}).get("state")
    return {"jobs": shown, "hidden": sum(1 for j in all_jobs if j["key"] in states),
            "computed_at": row["computed_at"], "seconds": row["seconds"], "stale": row["stale"],
            "refreshing": row["refreshing"], "note": p.get("note"), "gold_run": p.get("gold_run"),
            "timings": p.get("timings"), "near_low": p.get("near_low", NEAR_LOW)}


def mark_job(conn: psycopg.Connection, key: str, state: str | None, *, note: str | None = None) -> dict:
    """Mark a job done or dismissed (it is then not shown), or None to show it again."""
    if not isinstance(key, str) or not key or len(key) > 200:
        raise UnlockError("a job key")
    if state is None:
        conn.execute("delete from improvement_job where key = %s", (key,))
        return {"key": key, "state": None}
    if state not in ("done", "dismissed"):
        raise UnlockError("state is done or dismissed")
    field = key.split(":")[1] if key.startswith("focus:") and not key.startswith("focus:combined:") else None
    conn.execute("insert into improvement_job (key, state, field, note) values (%s, %s, %s, %s)"
                 " on conflict (key) do update set state = excluded.state, note = excluded.note, decided_at = now()",
                 (key, state, field, note))
    return {"key": key, "state": state}

