"""Enrichment step B: the answer key (gold set), labelled blind.

See docs/enrichment-plan.md §7 and §11 (decisions 3 and 4). No model and no network.

**The sample.** `sample(conn, n=300, seed=…)` draws a frozen, stratified set of items. Each
item is anchored on one message and carries the context it is judged in (its `unit`): the
pattern's other samples, the conversation, or a Teams window of messages with no gap over two
hours (window_first_id … window_last_id). The strata, for n = 300 (scaled for another n):

| Stratum  | Items | Drawn as                                                                  |
|----------|------:|---------------------------------------------------------------------------|
| boundary |    50 | the known hard cases: origin by `bulk_mailer` (15, step A's weakest rule), |
|          |       | by `correspondent` (8), receipts that also have a topic (7), a person via  |
|          |       | a platform (7), legacy "Get rid of" mail (6), bulk unknown senders (7)     |
| machine  |   100 | subject patterns of machine mail: big templates (40, drawn by size), the   |
|          |       | tail (30, patterns of one or two), origin undecided (30)                   |
| person   |   100 | person-side threads: the last 90 days (35), the last year (30), older (35) |
| teams    |    50 | two-hour windows: one-to-one (17), group (17) and meeting chats (16)       |

A group with too few candidates hands its share to the others in its stratum, then to any.
No message, thread, pattern or window is drawn twice. The draw is deterministic: the same
seed on the same archive gives the same items. The labelling order is shuffled by the seed, so
the strata never come in blocks. A set is written once and never changed; sampling again
makes a new set.

**Labels** go to `gold_label`, apart from assignments, so rules and models can be scored
against them. Each field takes a value (or several, for ask and route), "not sure" or "skip";
the note is optional. Nothing here writes an assignment. Every field's options are its
dimension's closed list (rules/taxonomy.json, loaded by `talos taxonomy load`), with each
value's family, label and description; route is a work category by name. A label whose value
has left its list (a route label holding an object id, from before route was a dimension) is
read as unset, through the view gold_label_valid, so the field is open again.

**Blind.** `item()` returns what a person needs to judge the message and nothing Talos has
decided about it: no assignment, no rule or pre-pass value, no importance, no automated flag,
no mailbox labels, and not the stratum or the reason it was picked. Those show only in
`reveal()`, the summary after a round of six, which refuses until every item in it is done.

**Fields per set.** A set labels the six fields origin, type, topic, ask, value and route unless
its params say otherwise (`params.label_fields`, a list from ALL_FIELDS; `kind` is the seventh).
The blind screen, progress ("done" is every one of the set's fields answered), the reveal, the
import, the check and the reports all read the set's fields (set_fields()).

**Sender groups** (sets drawn by sample_uncertain, sampler 2). Among the backfill cases Jev was
unsure of, a machine or person sender with GROUP_MIN (5) or more of them is one unit: its item is
one representative case (the one standing for the most messages), its info says how many cases
and messages the group holds, and gold_group_case freezes the member cases. The blind item shows
that it stands for N messages from this sender, with a few of their subjects and dates (never a
value). The owner can tick "mixed group" (the field `mixed`, a row meaning ticked); otherwise their
answer can later be given to every message of the group (propagate_groups, a dry run by default).

**Labellers.** Every answer has a labeller: the owner (personal.OWNER_ID, the default, and the only
one the blind screen, its progress and its sessions read or write) or another, such as `claude`,
whose answers are imported from JSONL (`import_labels`). The owner checks a frozen random sample of
another labeller's items (`check_sample`, `check_item`, `agree`); what they answer there is theirs,
like any other answer. `agreement()` compares two labellers field by field, and `report()` can take another
labeller's answers as the reference.

**Evaluation.** `compare(gold, predictions)` scores any predictor per field, per value, per
source and per confidence bucket; `report()` uses it for the rules and the pre-pass today, and
`predictions_from_run()` gives it a model run's proposals (Jev, step C).
"""

from __future__ import annotations

import json
import random
import re
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from talos import personal

from talos.enrich import MACHINE_ORIGINS, NOISE_TYPES, PERSON_ORIGINS, TEAMS_WINDOW_CAP, TEAMS_WINDOW_GAP

SAMPLER_VERSION = 1
OWNER = personal.OWNER_ID  # the labeller of the blind screen; every other labeller is compared, never shown there
FIELDS = ("origin", "type", "topic", "ask", "value", "route")   # a set's fields unless it names its own
ALL_FIELDS = FIELDS + ("kind",)                                  # every field an answer key may label
MANY_FIELDS = ("ask", "route")
MIXED = "mixed"                       # the owner's tick on a sender-group item: do not give the answer to the group
EXTRA_FIELDS = ("note", MIXED)        # stored as labels, never a set's field
GROUP_MIN = 5                         # unsure cases of one sender that make a sender group
EXAMPLES_MAX = 8                      # subjects shown for a sender group
SPLIT_SHARE = 0.2                     # a shared sender: tags or status words on at least this share of its cases
SPLIT_SHOWN = 25                      # shared senders (split by system) kept in a set's params for the report
STATUSES = ("set", "unsure", "skip")
ROUND = 6
SESSION_GAP = timedelta(minutes=30)
BUCKETS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.85), (0.85, 0.95), (0.95, 1.0))
TOPIC_MAX = 80
NOTE_MAX = 2000
CONTEXT_MAX = 60        # messages of a thread shown with an item
BODY_MAX = 20000        # characters of the anchor's text
CONTEXT_BODY_MAX = 6000

# (stratum, share of 300, [(group, share)]) in drawing order: the small, specific pools first.
STRATA = (
    ("boundary", 50, (("bulk_mailer", 15), ("correspondent", 8), ("receipt_topic", 7), ("platform_person", 7),
                      ("legacy_get_rid_of", 6), ("bulk_unknown", 7))),
    ("machine", 100, (("big", 40), ("tail", 30), ("undecided", 30))),
    ("person", 100, (("recent", 35), ("year", 30), ("older", 35))),
    ("teams", 50, (("oneOnOne", 17), ("group", 17), ("meeting", 16))),
)
REASONS = {
    "bulk_mailer": "origin set by the pre-pass signal bulk_mailer",
    "correspondent": "origin set by the pre-pass signal correspondent",
    "receipt_topic": "a receipt, invoice, order or booking that also has a topic",
    "platform_person": "a person through a platform (origin person_via_system)",
    "legacy_get_rid_of": "tagged legacy:get-rid-of",
    "bulk_unknown": "an unknown sender with 20+ messages, no automated headers, never answered",
    "big": "a big template",
    "tail": "the long tail: a pattern of one or two",
    "undecided": "machine mail whose origin is undecided",
    "recent": "a person thread from the last 90 days",
    "year": "a person thread from 90 days to a year ago",
    "older": "a person thread older than a year",
    "oneOnOne": "a one-to-one chat window",
    "group": "a group chat window",
    "meeting": "a meeting chat window",
}


class GoldError(ValueError):
    pass


# ---------------------------------------------------------------- the sample

def _split(total: int, weights: list[int]) -> list[int]:
    """total split in proportion to weights, largest remainder first; the parts sum to total."""
    s = sum(weights) or 1
    raw = [total * w / s for w in weights]
    parts = [int(r) for r in raw]
    for i in sorted(range(len(raw)), key=lambda i: (-(raw[i] - parts[i]), i))[:total - sum(parts)]:
        parts[i] += 1
    return parts


# Effective origin per message, ranked as effective_message_assignment ranks (message and thread).
_ORIGIN_CTE = """
o as (
    select distinct on (x.message_id) x.message_id, x.value, x.source_ref from (
        select a.entity_id as message_id, a.value, a.source_kind, a.source_ref, a.created_at, a.id, true as own
        from assignment a where a.dimension_id = 'origin' and a.status = 'active'
        union all
        select m.id, a.value, a.source_kind, a.source_ref, a.created_at, a.id, false
        from assignment a join message m on m.thread_id = a.entity_id
        where a.dimension_id = 'origin' and a.status = 'active'
    ) x
    order by x.message_id, case x.source_kind when 'human' then 4 when 'rule' then 3 when 'model' then 2 else 1 end desc,
             x.own desc, x.created_at desc, x.id desc),
ev as (select distinct message_id from event),
noise as (select distinct entity_id as message_id from assignment
          where dimension_id = 'type' and status = 'active' and value = any(%(noise)s))"""
# Machine mail: a machine origin, or no origin and a machine signal (the plan's §0 definition).
_MACHINE = ("(o.value = any(%(machine)s) or (o.value is null and (m.is_automated or ev.message_id is not null"
            " or noise.message_id is not null)))")
_PERSON = ("(o.value = any(%(person)s) or (o.value is null and not m.is_automated and ev.message_id is null"
           " and noise.message_id is null))")
_JOINS = ("left join o on o.message_id = m.id left join ev on ev.message_id = m.id"
          " left join noise on noise.message_id = m.id")


def _pools(conn: psycopg.Connection) -> dict[str, list[dict]]:
    """Every candidate per group, in a fixed order. Plain SELECTs, so a dry run works read-only."""
    p = {"noise": list(NOISE_TYPES), "machine": list(MACHINE_ORIGINS), "person": list(PERSON_ORIGINS)}
    pools: dict[str, list[dict]] = {}

    def rows(sql: str, **extra) -> list[dict]:
        return conn.execute(f"with {_ORIGIN_CTE}, {sql}", {**p, **extra}).fetchall()

    # ---- machine: subject patterns of machine mail
    pats = rows(f"""
        mm as (select m.id, mp.pattern_key, o.value is null as undecided
               from message m join message_pattern mp on mp.message_id = m.id {_JOINS}
               where m.direction = 'in' and m.medium = 'email' and {_MACHINE})
        select pattern_key, count(*)::int as n, array_agg(id order by id) as ids,
               coalesce(array_agg(id order by id) filter (where undecided), '{{}}') as undecided_ids
        from mm group by 1 order by 1""")
    pools["big"] = [{"kind": "pattern", "key": r["pattern_key"], "ids": r["ids"], "n": r["n"], "weight": r["n"]}
                    for r in pats if r["n"] >= 3]
    pools["tail"] = [{"kind": "pattern", "key": r["pattern_key"], "ids": r["ids"], "n": r["n"]}
                     for r in pats if r["n"] < 3]
    pools["undecided"] = [{"kind": "pattern", "key": r["pattern_key"], "ids": r["undecided_ids"], "n": r["n"]}
                          for r in pats if r["undecided_ids"]]

    # ---- person: threads of person-side mail, by the age of their latest person message
    threads = rows(f"""
        pm as (select m.id, coalesce(m.thread_id, -m.id) as tkey, m.received_at
               from message m {_JOINS}
               where m.direction = 'in' and m.medium = 'email' and {_PERSON})
        select tkey, array_agg(id order by id) as ids, count(*)::int as n,
               extract(epoch from now() - max(received_at)) / 86400 as age_days
        from pm group by 1 order by 1""")
    for name, lo, hi in (("recent", None, 90), ("year", 90, 365), ("older", 365, None)):
        pools[name] = [{"kind": "thread", "key": r["tkey"], "ids": r["ids"], "n": r["n"]} for r in threads
                       if (lo is None or (r["age_days"] or 0) > lo) and (hi is None or (r["age_days"] or 0) <= hi)]

    # ---- teams: two-hour windows, split every TEAMS_WINDOW_CAP messages, with an incoming message
    windows = conn.execute(f"""
        with t as (select id, thread_id, received_at, direction, headers->>'x-teams-chat-type' as chat_type,
                          case when received_at - lag(received_at) over (partition by thread_id order by received_at, id)
                                    <= interval '{TEAMS_WINDOW_GAP}' then 0 else 1 end as starts
                   from message where medium <> 'email' and thread_id is not null),
             w as (select *, sum(starts) over (partition by thread_id order by received_at, id rows unbounded preceding) as w
                   from t),
             c as (select *, (row_number() over (partition by thread_id, w order by received_at, id) - 1)
                             / {TEAMS_WINDOW_CAP} as part from w)
        select thread_id, w, part, array_agg(id order by received_at, id) as ids,
               coalesce(array_agg(id order by received_at, id) filter (where direction = 'in'), '{{}}') as incoming,
               coalesce(max(chat_type), 'group') as chat_type
        from c group by 1, 2, 3 order by 1, 2, 3""").fetchall()
    for name in ("oneOnOne", "group", "meeting"):
        pools[name] = [{"kind": "window", "key": (r["thread_id"], r["w"], r["part"]), "ids": r["incoming"],
                        "window": r["ids"], "n": len(r["ids"]), "chat_type": r["chat_type"]}
                       for r in windows if r["incoming"]
                       and (r["chat_type"] if r["chat_type"] in ("oneOnOne", "meeting") else "group") == name]

    # ---- boundary: the known hard cases
    def by(sql: str, group: str, key: str = "mp.pattern_key", **extra) -> None:
        found = rows(f"""
            b as (select m.id, coalesce({key}, 'm' || m.id) as k from message m
                  left join message_pattern mp on mp.message_id = m.id {_JOINS}
                  where m.direction = 'in' and {sql})
            select k, array_agg(id order by id) as ids, count(*)::int as n from b group by 1 order by 1""", **extra)
        pools[group] = [{"kind": "boundary", "key": r["k"], "ids": r["ids"], "n": r["n"]} for r in found]

    by("o.source_ref like 'prepass:origin.bulk_mailer@%%'", "bulk_mailer")
    by("o.source_ref like 'prepass:origin.correspondent@%%'", "correspondent", key="m.thread_id::text")
    by("""exists (select 1 from assignment a where a.entity_id in (m.id, m.thread_id) and a.status = 'active'
                  and a.dimension_id = 'type' and a.value in ('receipt', 'invoice', 'order', 'booking'))
          and exists (select 1 from assignment a where a.entity_id in (m.id, m.thread_id) and a.status = 'active'
                      and a.dimension_id = 'topic')""", "receipt_topic")
    by("o.value = 'person_via_system'", "platform_person")
    by("""exists (select 1 from assignment a where a.entity_id in (m.id, m.thread_id) and a.status = 'active'
                  and a.dimension_id = 'tag' and a.value = 'legacy:get-rid-of')""", "legacy_get_rid_of")
    by(f"""m.medium = 'email' and o.value is null and {_PERSON} and exists (select 1 from sender_profile sp
           where sp.account_id = m.account_id and sp.address = m.from_address and sp.message_count >= 20
           and sp.written_to_all = 0 and sp.replied_threads = 0)""", "bulk_unknown", key="m.from_address")
    return pools


def _order(rng: random.Random, pool: list[dict]) -> list[dict]:
    """The pool in the order it is drawn: by size for weighted pools (Efraimidis–Spirakis, so a
    template with more mail is likelier to come first), otherwise shuffled."""
    if pool and "weight" in pool[0]:
        keyed = [(rng.random() ** (1.0 / max(1, c["weight"])), i) for i, c in enumerate(pool)]
        return [pool[i] for _, i in sorted(keyed, key=lambda k: (-k[0], k[1]))]
    out = list(pool)
    rng.shuffle(out)
    return out


def _resolve(conn: psycopg.Connection, stratum: str, group: str, cand: dict, anchor: int) -> dict:
    """The item for an anchor: its unit and context, and the facts kept (hidden) in info."""
    m = conn.execute(
        "select m.id, m.thread_id, m.received_at, mp.pattern_key, t.message_count as thread_n,"
        " sp.message_count as pattern_n, sp.thread_count as pattern_threads, sp.sample_ids"
        " from message m left join thread t on t.id = m.thread_id"
        " left join message_pattern mp on mp.message_id = m.id"
        " left join subject_pattern sp on sp.pattern_key = mp.pattern_key where m.id = %s", (anchor,)).fetchone()
    item = {"stratum": stratum, "message_id": anchor, "thread_id": m["thread_id"], "pattern_key": m["pattern_key"],
            "context_ids": [], "window_first_id": None, "window_last_id": None,
            "info": {"group": group, "sampler": SAMPLER_VERSION}}
    samples = [i for i in (m["sample_ids"] or []) if i != anchor][:3]
    if cand["kind"] == "window":  # a chat line has no subject pattern; its unit is the window
        item.update(unit="window", window_first_id=cand["window"][0], window_last_id=cand["window"][-1],
                    pattern_key=None)
        item["info"].update(window_messages=cand["n"], chat_type=cand["chat_type"])
        detail = f"{cand['n']} {'message' if cand['n'] == 1 else 'messages'}"
    elif cand["kind"] == "thread" or (cand["kind"] == "boundary" and (m["thread_n"] or 0) > 1):
        item["unit"] = "thread" if (m["thread_n"] or 0) > 1 else "message"
        item["info"]["thread_messages"] = m["thread_n"] or 1
        detail = f"thread of {m['thread_n'] or 1}"
    elif (m["pattern_n"] or 0) > 1:
        item.update(unit="pattern", context_ids=samples)
        item["info"].update(pattern_messages=m["pattern_n"], pattern_threads=m["pattern_threads"])
        detail = f"pattern of {m['pattern_n']:,} messages".replace(",", " ")
    else:
        item["unit"] = "message"
        detail = "a single message"
    item["reason"] = f"{stratum} · {REASONS[group]} ({detail})"
    return item


def sample(conn: psycopg.Connection, *, n: int = 300, seed: int | None = None, name: str | None = None,
           dry_run: bool = False) -> dict:
    """Draw a new answer key and store it (unless dry_run). Returns the set with its counts.

    seed defaults to one more than the highest seed used so far (1 for the first set), so a new
    set differs from the old ones unless a seed is given. Existing sets are never touched."""
    if n < 1:
        raise GoldError("n must be at least 1")
    started = time.monotonic()
    if seed is None:
        seed = conn.execute("select coalesce(max(seed), 0) + 1 as s from gold_set").fetchone()["s"]
    rng = random.Random(seed)
    pools = _pools(conn)
    strata = _split(n, [share for _, share, _ in STRATA])
    groups: list[dict] = []
    for (stratum, _, subs), want in zip(STRATA, strata):
        for (group, _), k in zip(subs, _split(want, [s for _, s in subs])):
            groups.append({"stratum": stratum, "group": group, "want": k, "got": [],
                           "queue": deque(_order(rng, pools.get(group, []))), "pool": len(pools.get(group, []))})
    used: set = set()

    def take(g: dict) -> bool:
        """Add the group's next free candidate; False when it has none left. An item claims its
        message, its thread (a Teams window only its window) and its pattern, so none of them is
        drawn twice."""
        while g["queue"]:
            cand = g["queue"].popleft()
            free = [i for i in cand["ids"] if ("m", i) not in used]
            if ("k", cand["kind"], cand["key"]) in used or not free:
                continue
            anchor = rng.choice(free)
            item = _resolve(conn, g["stratum"], g["group"], cand, anchor)
            claims = {("m", anchor), ("k", cand["kind"], cand["key"])}
            if item["thread_id"] and cand["kind"] != "window":
                claims.add(("t", item["thread_id"]))
            if item["pattern_key"]:
                claims.add(("p", item["pattern_key"]))
            if claims & used:
                continue
            used.update(claims)
            g["got"].append(item)
            return True
        return False

    for g in groups:                                    # 1. each group its share
        while len(g["got"]) < g["want"] and take(g):
            pass
    for scope in ("stratum", "any"):                    # 2. a shortfall goes to its stratum, then anywhere
        for g in groups:
            short = g["want"] - len(g["got"])
            donors = [o for o in groups if o is not g and (scope == "any" or o["stratum"] == g["stratum"])]
            turn = 0
            while short > 0 and donors:
                o = donors[turn % len(donors)]
                if take(o):
                    short, turn = short - 1, turn + 1
                    g["want"] -= 1
                    o["want"] += 1
                else:
                    donors.remove(o)
    items = [i for g in groups for i in g["got"]]
    rng.shuffle(items)
    counts = Counter(i["stratum"] for i in items)
    out = {"seed": seed, "target": n, "items": len(items), "dry_run": dry_run,
           "strata": {s: counts.get(s, 0) for s, _, _ in STRATA},
           "groups": {g["group"]: {"stratum": g["stratum"], "items": len(g["got"]), "pool": g["pool"]} for g in groups},
           "units": dict(Counter(i["unit"] for i in items))}
    if not dry_run:
        with conn.transaction():
            sid = conn.execute(
                "insert into gold_set (name, seed, target, params) values (%s, %s, %s, %s) returning id",
                (name or f"Answer key, seed {seed}", seed, n,
                 Jsonb({"sampler": SAMPLER_VERSION, "strata": {s: k for (s, _, _), k in zip(STRATA, strata)},
                        "groups": out["groups"]}))).fetchone()["id"]
            for pos, i in enumerate(items, 1):
                conn.execute(
                    "insert into gold_item (set_id, position, message_id, stratum, reason, unit, thread_id, pattern_key,"
                    " context_ids, window_first_id, window_last_id, info)"
                    " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (sid, pos, i["message_id"], i["stratum"], i["reason"], i["unit"], i["thread_id"], i["pattern_key"],
                     i["context_ids"], i["window_first_id"], i["window_last_id"], Jsonb(i["info"])))
        out["set_id"] = sid
    out["seconds"] = round(time.monotonic() - started, 2)
    return out


# ---------------------------------------------------------------- labelling (the blind API)

def set_fields(conn: psycopg.Connection, set_id: int) -> list[str]:
    """The fields a set labels: params.label_fields, or the six by default."""
    row = conn.execute("select params from gold_set where id = %s", (set_id,)).fetchone()
    if not row:
        raise GoldError(f"no answer key {set_id}")
    return _fields_of(row["params"])


def _fields_of(params: dict | None) -> list[str]:
    want = (params or {}).get("label_fields")
    if isinstance(want, list) and want and all(f in ALL_FIELDS for f in want):
        return list(dict.fromkeys(want))
    return list(FIELDS)


def check_fields(fields) -> list[str]:
    """A set's fields from a list or 'origin,kind,…': known, no repeats, at least one."""
    if isinstance(fields, str):
        fields = [f.strip() for f in fields.split(",") if f.strip()]
    fields = list(dict.fromkeys(fields or []))
    bad = [f for f in fields if f not in ALL_FIELDS]
    if bad or not fields:
        raise GoldError(f"the fields are some of {', '.join(ALL_FIELDS)} (not {', '.join(bad) or 'none'})")
    return fields


def _done_count(conn: psycopg.Connection, set_id: int, fields: list[str], labeller: str = OWNER) -> int:
    return conn.execute(
        "select count(*)::int as n from (select l.item_id from gold_label_valid l where l.set_id = %s"
        " and l.field = any(%s) and l.labeller = %s group by 1 having count(*) = %s) d",
        (set_id, fields, labeller, len(fields))).fetchone()["n"]


def sets(conn: psycopg.Connection, *, archived: bool = False) -> list[dict]:
    """The answer keys, newest first, each with its fields and how many items the owner has done (every
    one of its fields answered). An archived set (params.archived) is left out of the screen but
    kept: reports still measure against it."""
    rows = conn.execute(
        "select s.id, s.name, s.seed, s.target, s.created_at, s.params, count(i.id)::int as items,"
        " coalesce((s.params ->> 'archived')::boolean, false) as archived"
        " from gold_set s left join gold_item i on i.set_id = s.id"
        " where %s or not coalesce((s.params ->> 'archived')::boolean, false)"
        " group by s.id order by s.id desc", (archived,)).fetchall()
    for r in rows:
        r["fields"] = _fields_of(r.pop("params"))
        r["done"] = _done_count(conn, r["id"], r["fields"])
    return rows


def archive(conn: psycopg.Connection, set_id: int, archived: bool = True) -> dict:
    """Take a set off the answer-key screen (or put it back). Its labels stay; reports still read it."""
    row = conn.execute("update gold_set set params = params || jsonb_build_object('archived', %s) where id = %s"
                       " returning id, name", (archived, set_id)).fetchone()
    if not row:
        raise GoldError(f"no answer key {set_id}")
    return {"set_id": row["id"], "name": row["name"], "archived": archived}


UNCERTAIN_FIELDS = {"origin": 0.70, "type": 0.70, "topic": 0.70, "value": 0.70}
UNCERTAIN_SAMPLER = 2
# How a set of n unsure items is made up (sampler 2): sender groups, single cases (the machine and
# person tail) and Teams windows; for n = 15: 6, 5 and 4.
UNCERTAIN_SHARES = {"group": 6 / 15, "teams": 4 / 15}


def uncertain_cases(conn: psycopg.Connection, *, exclude: set[int] | None = None) -> list[dict]:
    """The backfill cases Jev was unsure of: origin, type, topic or value under its accept level
    (0.70) or with a margin under 0.15. With the anchor's sender. A case that touches a message in
    exclude (its anchor or any member) is left out. Plain SELECTs."""
    rows = conn.execute(
        "select c.id, c.stage, c.unit_kind, c.unit_key, c.sample_no, c.anchor_id, c.member_ids,"
        " lower(m.from_address) as sender, m.subject, x.unsure"
        " from (select c.id, array_agg(p.field order by p.field) filter (where not p.fixed and p.field = any(%s)"
        "         and (coalesce(p.confidence, 0) < (%s::jsonb ->> p.field)::float or coalesce(p.margin, 0) < 0.15))"
        "         as unsure"
        "       from enrich_case c join enrich_prediction p on p.case_id = c.id"
        "       where c.error is null and c.run_id like 'jev-backfill-%%' group by c.id) x"
        " join enrich_case c on c.id = x.id join message m on m.id = c.anchor_id"
        " where x.unsure is not null order by c.id",
        (list(UNCERTAIN_FIELDS), Jsonb(UNCERTAIN_FIELDS))).fetchall()
    if exclude:
        rows = [r for r in rows if r["anchor_id"] not in exclude and not exclude.intersection(r["member_ids"] or ())]
    return rows


_REPLY = re.compile(r"^\s*((re|sv|fw|fwd|vs|vb|aw)\s*:\s*)+", re.I)
_TAG = re.compile(r"^\s*(\[[^\]]*\])(\w+)?")
_STATUS = re.compile(r"(?:^|\s)(down|up|alarm|alert|error|warning|failed|failure|critical|ok|recovered|resolved)(!*)\s*$",
                     re.I)


def system_key(subject: str | None) -> str:
    """Which system of a shared sender a subject comes from (one robot address sends for a dozen): its head.

    1. a leading [bracketed tag], with a word glued straight after it ('[backup5…]Active'); a
       run of four or more digits in the tag is a number, not a name (ticket ids merge);
    2. otherwise a trailing status word ('dc4 Down!' → '… Down!'), so every host's alarm is one;
    3. otherwise the first word, its digits replaced by #.
    Reply and forward prefixes are dropped first. [Success], [Warning] and [Failed] stay apart."""
    return _system_head(subject)[0]


def _system_head(subject: str | None) -> tuple[str, int]:
    """(head, the rule that gave it: 1 tag, 2 status word, 3 first word)."""
    text = _REPLY.sub("", subject or "").strip()
    m = _TAG.match(text)
    if m:
        return re.sub(r"\d{4,}", "#", m.group(1)) + (m.group(2) or ""), 1
    m = _STATUS.search(text)
    if m and text[:m.start()].strip():
        return f"… {m.group(1).capitalize()}{m.group(2)}", 2
    first = text.split()[0] if text.split() else "(no subject)"
    return re.sub(r"\d", "#", first), 3


def uncertain_units(rows: list[dict], group_min: int = GROUP_MIN) -> dict[str, list[dict]]:
    """The unsure cases as units: 'group' (group_min or more cases of one sender and system: its
    representative is the case standing for the most messages, then the lowest id), 'single' (the
    other machine and person cases) and 'teams' (one per window). Also 'split': per sender whose
    cases span more than one head, its heads, groups and cases.

    The system (system_key) splits a shared sender: one whose cases carry two or more different
    [tags] or status words, on at least SPLIT_SHARE (a fifth) of them. Such a sender (an IT robot
    address, GitHub, backupalert@; on either side, the robot address also lands with the person mail when its origin
    is undecided) is grouped by sender and head, the first word being the head where no tag or
    status word is. Any other sender stays one group whatever its subjects: a newsletter's or a
    person's subjects each start with a different word, and the first-word rule alone would cut
    them into pieces ('Du', 'Vad') and lose the grouping (measured on an archive:
    splitting every sender with more than one head left 12% of the machine cases in groups, this
    rule 75% of all machine and person cases). The owner's "mixed group" tick is for a group that is not
    one thing."""
    by_sender: dict[str, list[dict]] = defaultdict(list)
    out: dict[str, list] = {"group": [], "single": [], "teams": []}
    for r in rows:
        if r["stage"] == "teams":
            out["teams"].append({"case": r, "stage": "teams"})
        elif not r["sender"]:
            out["single"].append({"case": r, "stage": r["stage"]})
        else:  # one sender, whichever pool its cases fell in (a robot address is in both)
            by_sender[r["sender"]].append(r)
    by_key: dict[tuple, list[dict]] = {}
    split = {}
    for sender, cs in sorted(by_sender.items()):
        heads = {r["id"]: _system_head(r.get("subject")) for r in cs}
        tagged = [h for h, rule in heads.values() if rule < 3]
        if len(set(tagged)) >= 2 and len(tagged) >= SPLIT_SHARE * len(cs):
            sub: dict[str, list[dict]] = defaultdict(list)
            for r in cs:
                sub[heads[r["id"]][0]].append(r)
            for head, rs in sub.items():
                by_key[(sender, head)] = rs
            split[sender] = {"heads": len(sub), "cases": len(cs),
                             "groups": sum(1 for rs in sub.values() if len(rs) >= group_min),
                             "grouped_cases": sum(len(rs) for rs in sub.values() if len(rs) >= group_min)}
        else:
            by_key[(sender, None)] = cs
    for (sender, head), cs in by_key.items():
        if len(cs) >= group_min:
            rep = min(cs, key=lambda r: (-len(r["member_ids"] or ()), r["id"]))
            msgs = {m for r in cs for m in (r["member_ids"] or ())}
            out["group"].append({"case": rep, "stage": rep["stage"], "sender": sender, "system": head,
                                 "cases": sorted(r["id"] for r in cs), "messages": len(msgs),
                                 "stages": dict(Counter(r["stage"] for r in cs))})
        else:
            out["single"] += [{"case": r, "stage": r["stage"]} for r in cs]
    out["split"] = split
    return out


def _weighted(rng: random.Random, units: list[dict], k: int) -> list[dict]:
    """k units drawn without replacement, likelier the more messages they stand for
    (Efraimidis–Spirakis), deterministic for a seed."""
    keyed = sorted(((rng.random() ** (1.0 / max(1, u["messages"])), i) for i, u in enumerate(units)),
                   key=lambda x: (-x[0], x[1]))
    return [units[i] for _, i in keyed[:k]]


def sample_uncertain(conn: psycopg.Connection, *, n: int = 15, seed: int | None = None, name: str | None = None,
                     fields=FIELDS, groups: bool = True, dry_run: bool = False) -> dict:
    """A small answer key drawn from the backfill cases Jev was unsure about (uncertain_cases).

    With groups (sampler 2, set 3 on): about 40% sender groups (drawn by the messages they stand
    for, so the big senders come up), a third single cases (machine and person, half each) and the
    rest Teams windows; a pool that runs short hands its share to the others. Without groups: about
    40% machine, 40% person, 20% Teams cases. Every message already in an answer key is left out
    (a case touching one is never drawn), and a seed an earlier set used is refused, so a new set
    never repeats an old one. The same seed on the same archive draws the same items. Each item
    keeps its backfill case id (hidden); a group item also its sender, sizes and member cases
    (gold_group_case). fields: what the set labels (params.label_fields)."""
    fields = check_fields(fields)
    if n < 1:
        raise GoldError("n must be at least 1")
    used_seeds = {r["seed"] for r in conn.execute("select seed from gold_set")}
    if seed is not None and seed in used_seeds:
        raise GoldError(f"seed {seed} was used by an earlier answer key; pick another")
    while seed is None or seed in used_seeds:
        seed = random.SystemRandom().randrange(1, 10**6)
    rng = random.Random(seed)
    earlier = {r["message_id"] for r in conn.execute("select message_id from gold_item")}
    rows = uncertain_cases(conn, exclude=earlier)
    picked: list[dict] = []
    split: dict = {}
    if groups:
        units = uncertain_units(rows)
        split = dict(sorted(units.pop("split").items(), key=lambda kv: (-kv[1]["cases"], kv[0]))[:SPLIT_SHOWN])
        k_group = round(n * UNCERTAIN_SHARES["group"])
        k_teams = round(n * UNCERTAIN_SHARES["teams"])
        want = {"group": k_group, "single": n - k_group - k_teams, "teams": k_teams}
        pools = {k: len(v) for k, v in units.items()}
        picked += [{**u, "kind": "group"} for u in _weighted(rng, units["group"], want["group"])]
        single = {st: [u for u in units["single"] if u["stage"] == st] for st in ("machine", "person")}
        k_m = (want["single"] + 1) // 2
        take = {"machine": min(k_m, len(single["machine"]))}
        take["person"] = min(want["single"] - take["machine"], len(single["person"]))
        take["machine"] = min(want["single"] - take["person"], len(single["machine"]))
        for st in ("machine", "person"):
            picked += [{**u, "kind": "single"} for u in rng.sample(single[st], take[st])]
        picked += [{**u, "kind": "single"} for u in rng.sample(units["teams"], min(want["teams"], len(units["teams"])))]
        short = n - len(picked)
        if short > 0:  # a short pool hands its share to what is left, singles and windows alike
            chosen = {u["case"]["id"] for u in picked}
            rest = [u for u in units["single"] + units["teams"] if u["case"]["id"] not in chosen]
            picked += [{**u, "kind": "single"} for u in rng.sample(rest, min(short, len(rest)))]
    else:
        units = {"single": [{"case": r, "stage": r["stage"]} for r in rows]}
        want = {"machine": round(n * 0.4), "person": round(n * 0.4)}
        want["teams"] = n - want["machine"] - want["person"]
        pools = {st: sum(1 for r in rows if r["stage"] == st) for st in want}
        for st, k in want.items():
            pool = [u for u in units["single"] if u["stage"] == st]
            picked += [{**u, "kind": "single"} for u in rng.sample(pool, min(k, len(pool)))]
    rng.shuffle(picked)
    items = [_uncertain_item(conn, u) for u in picked]
    out = {"seed": seed, "items": len(items), "fields": fields, "dry_run": dry_run,
           "anchors": [i["message_id"] for i in items],
           "strata": dict(Counter(i["stratum"] for i in items)), "want": want, "pools": pools,
           "groups": [{"sender": u["sender"], "system": u["system"], "stage": u["stage"], "cases": len(u["cases"]),
                       "messages": u["messages"]} for u in picked if u["kind"] == "group"],
           "split_senders": split,
           "excluded_messages": len(earlier)}
    if dry_run:
        return out
    with conn.transaction():
        sid = conn.execute(
            "insert into gold_set (name, seed, target, params) values (%s, %s, %s, %s) returning id",
            (name or f"Jev unsure, {n} items, seed {seed}", seed, n,
             Jsonb({"kind": "uncertain", "sampler": UNCERTAIN_SAMPLER if groups else 1, "label_fields": fields,
                    "unsure_levels": UNCERTAIN_FIELDS, "groups": groups, "want": want, "pools": pools,
                    "split_senders": split}))
        ).fetchone()["id"]
        for pos, (i, u) in enumerate(zip(items, picked), 1):
            iid = conn.execute(
                "insert into gold_item (set_id, position, message_id, stratum, reason, unit, thread_id, pattern_key,"
                " context_ids, window_first_id, window_last_id, info)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id",
                (sid, pos, i["message_id"], i["stratum"], i["reason"], i["unit"], i["thread_id"], i["pattern_key"],
                 i["context_ids"], i["window_first_id"], i["window_last_id"], Jsonb(i["info"]))).fetchone()["id"]
            if u["kind"] == "group":
                with conn.cursor() as cur:
                    cur.executemany("insert into gold_group_case (item_id, case_id) values (%s, %s)",
                                    [(iid, c) for c in u["cases"]])
    out["set_id"] = sid
    return out


def _uncertain_item(conn: psycopg.Connection, u: dict) -> dict:
    """An answer-key item for an unsure case (a group's representative), as the backfill built it."""
    r = u["case"]
    m = conn.execute("select thread_id from message where id = %s", (r["anchor_id"],)).fetchone()
    members = list(r["member_ids"] or [])
    info = {"group": "sender" if u["kind"] == "group" else "uncertain", "case_id": r["id"],
            "unit_kind": r["unit_kind"], "unsure": r["unsure"]}
    reason = f"Jev unsure of {', '.join(r['unsure'])}"
    if u["kind"] == "group":
        info["sender_group"] = {"key": u["sender"] + (f" | {u['system']}" if u["system"] else ""),
                                "sender": u["sender"], "system": u["system"], "stage": u["stage"],
                                "cases": len(u["cases"]), "messages": u["messages"]}
        reason += (f" · sender group {u['sender']}" + (f" · {u['system']}" if u["system"] else "")
                   + f": {len(u['cases'])} cases, {u['messages']} messages")
    item = {"stratum": r["stage"], "message_id": r["anchor_id"], "thread_id": m["thread_id"], "pattern_key": None,
            "context_ids": [], "window_first_id": None, "window_last_id": None, "reason": reason, "info": info}
    if r["unit_kind"] == "window":
        item.update(unit="window", window_first_id=members[0], window_last_id=members[-1])
    elif r["unit_kind"] == "thread":
        item["unit"] = "thread"
    elif r["unit_kind"] == "template":
        others = [x["anchor_id"] for x in conn.execute(
            "select anchor_id from enrich_case where run_id like 'jev-backfill-%%' and unit_key = %s"
            " and anchor_id <> %s order by sample_no", (r["unit_key"], r["anchor_id"]))]
        item.update(unit="pattern", pattern_key=r["unit_key"], context_ids=list(dict.fromkeys(others))[:3])
    else:  # a tail template: the message alone, with its siblings as context when it has any
        sib = [x for x in members if x != r["anchor_id"]][:3]
        item.update(unit="pattern" if sib else "message", pattern_key=r["unit_key"] if sib else None,
                    context_ids=sib)
    return item


def group_of(conn: psycopg.Connection, item_row: dict) -> dict | None:
    """A sender-group item's group, blind: how many messages and cases it stands for, and up to
    EXAMPLES_MAX of its subjects with their dates, one per subject template (the biggest templates
    first; the anchor's own last), newest first. Never a value."""
    g = (item_row.get("info") or {}).get("sender_group")
    if not g:
        return None
    rows = conn.execute(
        """
        with msgs as (select unnest(c.member_ids) as id from gold_group_case gc
                      join enrich_case c on c.id = gc.case_id where gc.item_id = %(item)s
                      union select gm.message_id from gold_group_message gm where gm.item_id = %(item)s),
             p as (select m.id, m.subject, m.received_at,
                          coalesce(mp.pattern_key, lower(regexp_replace(coalesce(m.subject, ''),
                                   '^\\s*((re|sv|fw|fwd|vs|aw)\\s*:\\s*)+', '', 'i'))) as pk
                   from msgs join message m on m.id = msgs.id left join message_pattern mp on mp.message_id = m.id),
             per as (select pk, count(*) as n from p group by pk),
             latest as (select distinct on (pk) pk, id, subject, received_at from p
                        order by pk, received_at desc nulls last, id desc)
        select l.subject, l.received_at, per.n, l.pk = (select pk from p where id = %(anchor)s) as anchor_pk
        from latest l join per on per.pk = l.pk
        order by (l.pk = (select pk from p where id = %(anchor)s)) nulls first, per.n desc, l.received_at desc nulls last
        limit %(k)s""", {"item": item_row["id"], "anchor": item_row["message_id"], "k": EXAMPLES_MAX}).fetchall()
    examples = sorted(({"subject": r["subject"], "received_at": r["received_at"], "messages": r["n"]} for r in rows),
                      key=lambda x: (x["received_at"] is None, -(x["received_at"].timestamp()) if x["received_at"] else 0))
    return {"messages": g["messages"], "cases": g["cases"], "sender": g.get("sender") or g["key"],
            "system": g.get("system"), "examples": examples}


def options(conn: psycopg.Connection) -> dict:
    """What each field may be: its dimension's values in the file's order, each with its family,
    label and description. A topic with no list yet (the taxonomy not loaded) suggests the values
    in use instead, and takes new ones."""
    dims = {r["id"]: r for r in conn.execute(
        "select id, allowed, value_meta from dimension where id = any(%s)", (list(ALL_FIELDS),))}
    out = {"fields": list(ALL_FIELDS), "many": list(MANY_FIELDS), "statuses": list(STATUSES), "round": ROUND}
    # How often the owner has chosen each value in any answer key: it orders the Common group. It is
    # about the owner, never about the item in hand, so it keeps the labelling blind.
    used: dict[tuple[str, str], int] = {(r["field"], r["value"]): r["n"] for r in conn.execute(
        "select field, v as value, count(*)::int as n from gold_label, unnest(values) v"
        " where status = 'set' and labeller = %s group by 1, 2", (OWNER,))}
    for f in ALL_FIELDS:
        d = dims.get(f) or {"allowed": None, "value_meta": {}}
        values = list(d["allowed"] or [])
        if f == "topic":
            out["topic_closed"] = bool(values)
            if not values:
                values = [r["value"] for r in conn.execute(
                    "select value from assignment where dimension_id = 'topic' and status = 'active'"
                    " group by 1 order by count(*) desc, 1 limit 300")]
        meta = d["value_meta"] or {}
        out[f] = [{"value": v, "family": (meta.get(v) or {}).get("family", ""),
                   "label": (meta.get(v) or {}).get("label", v),
                   "description": (meta.get(v) or {}).get("description", ""),
                   "common": int((meta.get(v) or {}).get("common") or 0), "used": used.get((f, v), 0)}
                  for v in values]
    return out


def allowed(conn: psycopg.Connection, field: str) -> list[str] | None:
    """The field's closed list, or None when its dimension has none."""
    row = conn.execute("select allowed from dimension where id = %s", (field,)).fetchone()
    return list(row["allowed"]) if row and row["allowed"] is not None else None


def _item_row(conn: psycopg.Connection, set_id: int, position: int) -> dict:
    row = conn.execute("select * from gold_item where set_id = %s and position = %s", (set_id, position)).fetchone()
    if not row:
        raise GoldError(f"answer key {set_id} has no item {position}")
    return row


def _labels(conn: psycopg.Connection, item_ids: list[int], labeller: str = OWNER) -> dict[int, dict]:
    """One labeller's answers (the owner's, unless another is named), by item and field."""
    out: dict[int, dict] = defaultdict(dict)
    for r in conn.execute("select item_id, field, values, status, duration_ms, after_reveal from gold_label_valid"
                          " where item_id = any(%s) and labeller = %s", (item_ids, labeller)):
        out[r["item_id"]][r["field"]] = {"values": r["values"], "status": r["status"],
                                         "duration_ms": r["duration_ms"], "after_reveal": r["after_reveal"]}
    return out


# The only message columns the blind item shows: who, when, what it says, and its files.
_BLIND_COLUMNS = ("m.id, m.account_id, m.medium, m.received_at, m.direction, m.from_name, m.from_address,"
                  " m.subject, m.has_attachments, m.headers->>'x-teams-chat-type' as chat_type,"
                  " m.headers->>'x-teams-edited' as edited, m.headers->>'x-teams-deleted' as deleted,"
                  " m.headers->>'x-teams-importance' as teams_importance")


def _blind_messages(conn: psycopg.Connection, ids: list[int], *, body_max: int, full: bool = False) -> list[dict]:
    """Messages as the answer key shows them, in the order of ids."""
    if not ids:
        return []
    rows = {r["id"]: r for r in conn.execute(
        f"select {_BLIND_COLUMNS}, left(coalesce(t.body_text, ''), %s) as body_text,"
        " left(coalesce(t.quote_stripped, ''), %s) as quote_stripped, t.body_kind"
        " from message m left join message_text t on t.message_id = m.id where m.id = any(%s)",
        (body_max, body_max, ids))}
    people: dict[int, list] = defaultdict(list)
    for p in conn.execute("select message_id, role, address, name from participant where message_id = any(%s)"
                          " and role in ('to', 'cc') order by message_id, role desc, ordinal", (ids,)):
        people[p["message_id"]].append({"role": p["role"], "address": p["address"], "name": p["name"]})
    atts: dict[int, list] = defaultdict(list)
    for a in conn.execute("select id, message_id, filename, content_type, size_bytes,"
                          " jsonb_build_object('reference', attrs->'reference', 'url', attrs->'url', 'pages', attrs->'pages')"
                          " as attrs from attachment where message_id = any(%s) order by message_id, part_path", (ids,)):
        atts[a.pop("message_id")].append(a)
    out = []
    for i in ids:
        r = rows.get(i)
        if not r:
            continue
        r["participants"] = people.get(i, [])
        r["attachments"] = atts.get(i, [])
        if r["medium"] != "email":
            r["text"] = r["body_text"]
        if not full:
            r.pop("participants")
        out.append(r)
    return out


def unit(conn: psycopg.Connection, kind: str, message_id: int, *, thread_id: int | None = None,
         context_ids=(), pattern_key: str | None = None, window_first_id: int | None = None,
         window_last_id: int | None = None) -> dict:
    """A unit, blind: its anchor message and the context it is judged in, by message ids.

    kind is an answer-key unit: `message` (the anchor alone), `thread` (the thread's messages
    nearest the anchor, at most CONTEXT_MAX, and its size), `pattern` (the given other samples
    and the pattern's counts) or `window` (a Teams window from window_first_id to
    window_last_id in the anchor's chat, and the three messages before it). An answer-key item
    is one (item() below); the enrichment backfill builds its cases with it too, so a case and an
    answer-key item on the same messages give Jev the same record."""
    anchor = _blind_messages(conn, [message_id], body_max=BODY_MAX, full=True)
    out = {"unit": kind, "message": anchor[0] if anchor else None, "context": [], "before": []}
    if kind == "thread" and thread_id:
        ids = [r["id"] for r in conn.execute(
            "select id from (select id, received_at, abs(extract(epoch from received_at - %s)) as d from message"
            " where thread_id = %s order by d nulls last, id limit %s) x order by received_at nulls first, id",
            (anchor[0]["received_at"] if anchor else None, thread_id, CONTEXT_MAX))]
        out["context"] = _blind_messages(conn, ids, body_max=CONTEXT_BODY_MAX)
        out["thread_messages"] = conn.execute("select count(*)::int as n from message where thread_id = %s",
                                              (thread_id,)).fetchone()["n"]
    elif kind == "pattern":
        out["context"] = _blind_messages(conn, list(context_ids), body_max=CONTEXT_BODY_MAX)
        pat = conn.execute("select message_count, thread_count, first_at, last_at from subject_pattern"
                           " where pattern_key = %s", (pattern_key,)).fetchone()
        out["pattern"] = pat or {}
    elif kind == "window":
        bounds = conn.execute("select received_at, id from message where id in (%s, %s) order by received_at, id",
                              (window_first_id, window_last_id)).fetchall()
        lo, hi = bounds[0], bounds[-1]
        ids = [r["id"] for r in conn.execute(
            "select id from message where thread_id = %s and (received_at, id) >= (%s, %s) and (received_at, id) <= (%s, %s)"
            " order by received_at, id", (thread_id, lo["received_at"], lo["id"], hi["received_at"], hi["id"]))]
        before = [r["id"] for r in conn.execute(
            "select id from message where thread_id = %s and (received_at, id) < (%s, %s)"
            " order by received_at desc, id desc limit 3", (thread_id, lo["received_at"], lo["id"]))][::-1]
        out["context"] = _blind_messages(conn, ids, body_max=CONTEXT_BODY_MAX)
        out["before"] = _blind_messages(conn, before, body_max=CONTEXT_BODY_MAX)
        out["chat_type"] = anchor[0]["chat_type"] if anchor else None
    return out


def item(conn: psycopg.Connection, set_id: int, position: int) -> dict:
    """One item, blind: the message, its context, the set's fields and the owner's labels so far (with
    their note and, on a sender-group item, the group's size and a few subjects, and their "mixed" tick).
    Never an assignment, a rule or model value, an importance, an automated flag, a mailbox label,
    why it was picked, or another labeller's answer (a test holds this)."""
    it = _item_row(conn, set_id, position)
    fields = set_fields(conn, set_id)
    total = conn.execute("select count(*)::int as n from gold_item where set_id = %s", (set_id,)).fetchone()["n"]
    u = unit(conn, it["unit"], it["message_id"], thread_id=it["thread_id"], context_ids=it["context_ids"] or (),
             pattern_key=it["pattern_key"], window_first_id=it["window_first_id"], window_last_id=it["window_last_id"])
    out = {"set_id": set_id, "position": position, "total": total, "round": (position - 1) // ROUND + 1,
           "unit": it["unit"], "message": u["message"], "context": u["context"], "before": u["before"],
           "revealed": it["revealed_at"] is not None, "fields": fields, "group": group_of(conn, it)}
    for k in ("thread_messages", "pattern", "chat_type"):
        if k in u:
            out[k] = u[k]
    labels = {f: v for f, v in _labels(conn, [it["id"]]).get(it["id"], {}).items() if f in fields or f in EXTRA_FIELDS}
    out["labels"] = {f: {"values": v["values"], "status": v["status"]} for f, v in labels.items()}
    out["duration_ms"] = max([v["duration_ms"] or 0 for v in labels.values()] or [0])
    lo = (out["round"] - 1) * ROUND + 1
    out["round_items"] = [{"position": r["position"], "done": r["n"] == len(fields)} for r in conn.execute(
        "select i.position, count(l.field) filter (where l.field = any(%s))::int as n from gold_item i"
        " left join gold_label_valid l on l.item_id = i.id and l.labeller = %s"
        " where i.set_id = %s and i.position between %s and %s"
        " group by 1 order by 1", (fields, OWNER, set_id, lo, lo + ROUND - 1))]
    return out


def _check(conn: psycopg.Connection, field: str, values: list, status: str) -> list[str]:
    """The values, cleaned, or a GoldError that says what is wrong."""
    if field not in (*ALL_FIELDS, *EXTRA_FIELDS):
        raise GoldError(f"unknown field {field!r}; use one of {', '.join(ALL_FIELDS)}, note or {MIXED}")
    if status not in STATUSES:
        raise GoldError(f"unknown status {status!r}; use one of {', '.join(STATUSES)}")
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise GoldError("values is a list of strings")
    values = list(dict.fromkeys(v.strip() for v in values if v.strip()))
    if field == "note":
        if len(values) > 1 or (values and len(values[0]) > NOTE_MAX):
            raise GoldError(f"a note is one text of at most {NOTE_MAX} characters")
        return values
    if field == MIXED:
        if values not in ([], [MIXED]):
            raise GoldError(f"{MIXED} is ticked ([\"{MIXED}\"]) or not ([])")
        return values
    if field not in MANY_FIELDS and len(values) > 1:
        raise GoldError(f"{field} takes one value")
    if field not in MANY_FIELDS and status == "set" and not values:
        raise GoldError(f"choose a value for {field}, or mark it not sure or skipped")
    allowed_values = allowed(conn, field)
    if allowed_values is None:
        if field != "topic":
            raise GoldError(f"{field} has no value list yet; load it with: talos taxonomy load")
        if any(len(v) > TOPIC_MAX for v in values):
            raise GoldError(f"a topic is at most {TOPIC_MAX} characters")
    bad = [v for v in values if allowed_values is not None and v not in allowed_values]
    if bad:
        raise GoldError(f"{bad[0]!r} is not an allowed {field}")
    return values


def save_label(conn: psycopg.Connection, set_id: int, position: int, field: str, values: list,
               status: str = "set", duration_ms: int | None = None, labeller: str = OWNER) -> dict:
    """Save one answer (replacing the labeller's earlier one for that field). The field must be one
    of the set's (or the note, or the "mixed" tick of a sender-group item). An empty note, or an
    untick, removes it. Returns the item's labels and the set's progress (the owner's), still blind."""
    it = _item_row(conn, set_id, position)
    values = _check(conn, field, values, status)
    labeller = _labeller(labeller)
    fields = set_fields(conn, set_id)
    if field not in fields and field not in EXTRA_FIELDS:
        raise GoldError(f"answer key {set_id} does not label {field}; its fields are {', '.join(fields)}")
    if field == MIXED and not (it["info"] or {}).get("sender_group"):
        raise GoldError("only a sender-group item can be marked a mixed group")
    if duration_ms is not None:
        duration_ms = max(0, min(int(duration_ms), 24 * 3600 * 1000))
    with conn.transaction():
        if field in EXTRA_FIELDS and not values:
            conn.execute("delete from gold_label where item_id = %s and field = %s and labeller = %s",
                         (it["id"], field, labeller))
        else:
            conn.execute(
                "insert into gold_label (set_id, item_id, field, values, status, duration_ms, after_reveal, labeller)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (item_id, field, labeller) do update set values = excluded.values,"
                " status = excluded.status, labelled_at = now(), duration_ms = excluded.duration_ms,"
                " after_reveal = gold_label.after_reveal or excluded.after_reveal",
                (set_id, it["id"], field, values, status, duration_ms,
                 labeller == OWNER and it["revealed_at"] is not None, labeller))
    labels = _labels(conn, [it["id"]]).get(it["id"], {})
    return {"labels": {f: {"values": v["values"], "status": v["status"]} for f, v in labels.items()
                       if f in fields or f in EXTRA_FIELDS},
            "progress": progress(conn, set_id)}


def _labeller(name) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
        raise GoldError(f"a labeller is a short lowercase name, such as {OWNER} or claude (not {name!r})")
    return name


def _sessions(times: list[datetime], now: datetime) -> dict:
    """Labelling sessions: a gap of more than SESSION_GAP starts a new one."""
    starts = [t for i, t in enumerate(times) if i == 0 or t - times[i - 1] > SESSION_GAP]
    active = bool(times) and now - times[-1] <= SESSION_GAP
    return {"count": len(starts), "active": active, "number": len(starts) if active else len(starts) + 1,
            "started_at": starts[-1] if active else None}


def progress(conn: psycopg.Connection, set_id: int, labeller: str = OWNER) -> dict:
    """How far the labelling is: items done (every one of the set's fields answered), per field,
    the next item to do, and the sessions. The owner's, unless another labeller is named."""
    s = conn.execute("select id, name, seed, target, created_at from gold_set where id = %s", (set_id,)).fetchone()
    if not s:
        raise GoldError(f"no answer key {set_id}")
    fields = set_fields(conn, set_id)
    items = conn.execute(
        "select i.position, count(l.field) filter (where l.field = any(%s))::int as fields,"
        " max(l.labelled_at) filter (where l.field = any(%s)) as last_at"
        " from gold_item i left join gold_label_valid l on l.item_id = i.id and l.labeller = %s where i.set_id = %s"
        " group by i.position order by i.position", (fields, fields, labeller, set_id)).fetchall()
    done = [r for r in items if r["fields"] == len(fields)]
    per_field = {f: {"set": 0, "unsure": 0, "skip": 0} for f in fields}
    for r in conn.execute("select field, status, count(*)::int as n from gold_label_valid where set_id = %s"
                          " and field = any(%s) and labeller = %s group by 1, 2", (set_id, fields, labeller)):
        per_field[r["field"]][r["status"]] = r["n"]
    times = [r["labelled_at"] for r in conn.execute(
        "select labelled_at from gold_label where set_id = %s and labeller = %s order by labelled_at",
        (set_id, labeller))]
    now = datetime.now(timezone.utc)
    sess = _sessions(times, now)
    sess["items_done"] = sum(1 for r in done if sess["started_at"] and r["last_at"] >= sess["started_at"])
    nxt = next((r["position"] for r in items if r["fields"] < len(fields)), None)
    rounds = (len(items) + ROUND - 1) // ROUND
    return {"set": s, "total": len(items), "done": len(done), "next": nxt, "rounds": rounds,
            "rounds_done": sum(1 for k in range(rounds)
                               if all(r["fields"] == len(fields) for r in items[k * ROUND:(k + 1) * ROUND])),
            "fields": per_field, "label_fields": fields, "session": sess}


# ---------------------------------------------------------------- after a round: the reveal

RULE_FIELDS = ("origin", "type", "topic", "kind")


def kind_of(conn: psycopg.Connection) -> dict[str, str]:
    """{type: its kind}, from the loaded taxonomy (empty before kind is loaded)."""
    from talos import boundary
    try:
        return boundary.sides(conn).get(boundary.KIND, {}).get("side_of", {})
    except boundary.BoundaryError:
        return {}


def rule_predictions(conn: psycopg.Connection, set_id: int, fields=("origin", "type", "topic"),
                     item_ids: list[int] | None = None, *, kind: bool = False) -> dict[int, dict[str, "Prediction"]]:
    """What the rules and the pre-pass say about each item's message: per field the strongest
    rule-made value on the message or its thread (the message's own first, then the newest),
    with its source ('prepass origin.bulk_mailer', 'rule seed-x'). With kind: also the kind of
    the rule's type (no rule sets kind itself)."""
    rows = conn.execute(
        """
        select distinct on (i.id, a.dimension_id) i.id as item_id, a.dimension_id as field, a.value, a.source_ref
        from gold_item i join message m on m.id = i.message_id
        join assignment a on a.entity_id in (m.id, m.thread_id) and a.status = 'active' and a.source_kind = 'rule'
                          and a.dimension_id = any(%s)
        where i.set_id = %s and (%s::int[] is null or i.id = any(%s))
        order by i.id, a.dimension_id, (a.entity_id = m.id) desc, a.created_at desc, a.id desc
        """, (list(fields), set_id, item_ids, item_ids)).fetchall()
    out: dict[int, dict] = defaultdict(dict)
    for r in rows:
        out[r["item_id"]][r["field"]] = Prediction((r["value"],), 1.0, source_name(r["source_ref"]))
    if kind:
        ko = kind_of(conn)
        for preds in out.values():
            t = preds.get("type")
            if t is not None and ko.get(t.values[0]):
                preds["kind"] = Prediction((ko[t.values[0]],), 1.0, t.source)
    return dict(out)


def source_name(ref: str | None) -> str:
    """'prepass:origin.bulk_mailer@1' → 'prepass bulk_mailer'; 'rule:seed-x@2' → 'rule seed-x'."""
    if not ref:
        return "?"
    if ref.startswith("prepass:"):
        return "prepass " + ref.removeprefix("prepass:").split("@")[0].split(".", 1)[-1]
    if ref.startswith("rule:"):
        return "rule " + ref.removeprefix("rule:").split("@")[0]
    return ref


def reveal(conn: psycopg.Connection, set_id: int, round_no: int) -> dict:
    """The summary of a finished round: each item's labels beside what the rules and the pre-pass
    say, why it was picked, and the time spent. Refused until every item in the round is done;
    the first call marks the items revealed, so a later change to them is flagged."""
    lo, hi = (round_no - 1) * ROUND + 1, round_no * ROUND
    items = conn.execute(
        "select i.*, m.subject, m.from_name, m.from_address, m.medium from gold_item i"
        " join message m on m.id = i.message_id"
        " where i.set_id = %s and i.position between %s and %s order by i.position", (set_id, lo, hi)).fetchall()
    if not items:
        raise GoldError(f"answer key {set_id} has no round {round_no}")
    fields = set_fields(conn, set_id)
    labels = {i: {f: v for f, v in lab.items() if f in fields or f in EXTRA_FIELDS}
              for i, lab in _labels(conn, [i["id"] for i in items]).items()}
    unfinished = [i["position"] for i in items if len([f for f in labels.get(i["id"], {}) if f in fields]) < len(fields)]
    if unfinished:
        raise GoldError(f"round {round_no} is not finished (items {', '.join(map(str, unfinished))});"
                        " the rule values show only after it")
    conn.execute("update gold_item set revealed_at = now() where set_id = %s and position between %s and %s"
                 " and revealed_at is null", (set_id, lo, hi))
    preds = rule_predictions(conn, set_id, item_ids=[i["id"] for i in items], kind="kind" in fields)
    gold = {i["id"]: labels.get(i["id"], {}) for i in items}
    rows, agree = [], Counter()
    for i in items:
        lab = labels.get(i["id"], {})
        preds.setdefault(i["id"], {})
        preds[i["id"]] = {f: p for f, p in preds[i["id"]].items() if f in fields}
        row = {"position": i["position"], "subject": i["subject"], "from": i["from_name"] or i["from_address"],
               "medium": i["medium"], "stratum": i["stratum"], "reason": i["reason"], "unit": i["unit"],
               "labels": {f: {"values": v["values"], "status": v["status"]} for f, v in lab.items()},
               "duration_ms": max([v["duration_ms"] or 0 for v in lab.values()] or [0]), "rules": {}}
        for f, p in preds.get(i["id"], {}).items():
            g = lab.get(f)
            ok = None if not g or g["status"] != "set" else _same(f, p.values, g["values"])
            row["rules"][f] = {"value": p.values[0], "source": p.source, "agrees": ok}
            if ok is not None:
                agree[f, ok] += 1
        rows.append(row)
    fields_scored = [f for f in RULE_FIELDS if f in fields]
    return {"round": round_no, "items": rows,
            "seconds": round(sum(r["duration_ms"] for r in rows) / 1000),
            "unsure": sum(1 for g in gold.values() for f, v in g.items() if f in fields and v["status"] == "unsure"),
            "skipped": sum(1 for g in gold.values() for f, v in g.items() if f in fields and v["status"] == "skip"),
            "fields": fields,
            "agreement": {f: {"agree": agree[f, True], "compared": agree[f, True] + agree[f, False]}
                          for f in fields_scored}}


# ---------------------------------------------------------------- evaluation

@dataclass(frozen=True)
class Prediction:
    """One predictor's answer for one item and field: its value(s) (empty = none, for ask and
    route), its confidence (None when it has none) and where it came from."""
    values: tuple[str, ...]
    confidence: float | None = None
    source: str | None = None


def _norm(field: str, v: str) -> str:
    return v.strip().casefold() if field == "topic" else v


def _same(field: str, predicted, gold) -> bool:
    if field in MANY_FIELDS:
        return {_norm(field, v) for v in predicted} == {_norm(field, v) for v in gold}
    return bool(predicted) and bool(gold) and _norm(field, predicted[0]) == _norm(field, gold[0])


def bucket(confidence: float | None) -> str:
    """The calibration bucket of a confidence (plan §7): 0.00–0.50, 0.50–0.70, … 0.95–1.00."""
    if confidence is None:
        return "none"
    for lo, hi in BUCKETS:
        if confidence < hi:
            return f"{lo:.2f}–{hi:.2f}"
    lo, hi = BUCKETS[-1]
    return f"{lo:.2f}–{hi:.2f}"


def _rate(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def compare(gold: dict[int, dict[str, dict]], predictions: dict[int, dict[str, Prediction]],
            fields=FIELDS) -> dict[str, dict]:
    """Score predictions against the owner's labels, per field.

    gold: {item_id: {field: {"values": [...], "status": "set"|"unsure"|"skip"}}} (gold_labels()).
    predictions: {item_id: {field: Prediction}}; an item or field left out was not predicted.
    Only answers the owner gave for sure count; "not sure" and skipped fields are counted apart.

    One-value fields: how many were predicted (coverage), right (accuracy), and per predicted
    value its precision (right / predicted) and per gold value its recall (right / labelled);
    the wrong ones as "predicted→gold" pairs; the same per source and per confidence bucket.
    Many-value fields (ask, route): per value true and false positives and misses, precision
    and recall; "any" treats the field as yes/no (is there an ask at all?); exact is the
    share of items whose whole set matched."""
    out = {}
    for field in fields:
        many = field in MANY_FIELDS
        labelled = unsure = skipped = 0
        pairs = []  # (item, gold values, prediction)
        gold_values = Counter()
        for item_id, labels in gold.items():
            g = labels.get(field)
            if not g:
                continue
            if g["status"] != "set":
                unsure += g["status"] == "unsure"
                skipped += g["status"] == "skip"
                continue
            labelled += 1
            for v in (g["values"] if many else g["values"][:1]):
                gold_values[_norm(field, v)] += 1
            p = predictions.get(item_id, {}).get(field)
            if p is not None:
                pairs.append((item_id, g["values"], p))
        res = {"labelled": labelled, "unsure": unsure, "skipped": skipped, "predicted": len(pairs),
               "coverage": _rate(len(pairs), labelled)}
        by_source: dict[str, dict] = defaultdict(lambda: {"n": 0, "correct": 0, "wrong": Counter(), "wrong_items": []})
        by_bucket: dict[str, dict] = defaultdict(lambda: {"n": 0, "correct": 0})
        correct = 0
        if not many:
            per_value: dict[str, dict] = defaultdict(lambda: {"predicted": 0, "correct": 0})
            for item_id, g, p in pairs:
                pv, gv = _norm(field, p.values[0]) if p.values else "", _norm(field, g[0])
                ok = pv == gv
                correct += ok
                per_value[pv]["predicted"] += 1
                per_value[pv]["correct"] += ok
                s = by_source[p.source or "?"]
                s["n"] += 1
                s["correct"] += ok
                if not ok:
                    s["wrong"][f"{pv}→{gv}"] += 1
                    s["wrong_items"].append(item_id)
                b = by_bucket[bucket(p.confidence)]
                b["n"] += 1
                b["correct"] += ok
            for v, d in per_value.items():
                d["precision"] = _rate(d["correct"], d["predicted"])
                d["gold"] = gold_values.get(v, 0)
                d["recall"] = _rate(d["correct"], d["gold"])
            for v, n in gold_values.items():
                if v not in per_value:
                    per_value[v] = {"predicted": 0, "correct": 0, "precision": None, "gold": n, "recall": 0.0}
            res["per_value"] = dict(sorted(per_value.items(), key=lambda kv: (-kv[1]["predicted"], kv[0])))
        else:
            per_value = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
            anyv = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
            for item_id, g, p in pairs:
                gs, ps = {_norm(field, v) for v in g}, {_norm(field, v) for v in p.values}
                ok = gs == ps
                correct += ok
                for v in ps & gs:
                    per_value[v]["tp"] += 1
                for v in ps - gs:
                    per_value[v]["fp"] += 1
                for v in gs - ps:
                    per_value[v]["fn"] += 1
                anyv["tp" if gs and ps else "fp" if ps else "fn" if gs else "tn"] += 1
                s = by_source[p.source or "?"]
                s["n"] += 1
                s["correct"] += ok
                if not ok:
                    s["wrong"][f"{','.join(sorted(ps)) or 'none'}→{','.join(sorted(gs)) or 'none'}"] += 1
                    s["wrong_items"].append(item_id)
                b = by_bucket[bucket(p.confidence)]
                b["n"] += 1
                b["correct"] += ok
            for d in per_value.values():
                d["precision"] = _rate(d["tp"], d["tp"] + d["fp"])
                d["recall"] = _rate(d["tp"], d["tp"] + d["fn"])
            anyv["precision"] = _rate(anyv["tp"], anyv["tp"] + anyv["fp"])
            anyv["recall"] = _rate(anyv["tp"], anyv["tp"] + anyv["fn"])
            res["per_value"] = dict(sorted(per_value.items()))
            res["any"] = anyv
        res["correct"] = correct
        res["accuracy"] = _rate(correct, len(pairs))
        res["exact"] = res["accuracy"] if many else None
        for s in by_source.values():
            s["precision"] = _rate(s["correct"], s["n"])
            s["wrong"] = dict(s["wrong"].most_common())
        for b in by_bucket.values():
            b["precision"] = _rate(b["correct"], b["n"])
        res["by_source"] = dict(sorted(by_source.items(), key=lambda kv: (-kv[1]["n"], kv[0])))
        res["by_bucket"] = dict(sorted(by_bucket.items()))
        out[field] = res
    return out


def gold_labels(conn: psycopg.Connection, set_id: int, labeller: str = OWNER) -> dict[int, dict[str, dict]]:
    """A labeller's labels (the owner's by default) for compare(): {item_id: {field: {"values", "status"}}},
    the note left out."""
    out: dict[int, dict] = defaultdict(dict)
    for r in conn.execute("select item_id, field, values, status from gold_label_valid where set_id = %s"
                          " and field = any(%s) and labeller = %s", (set_id, list(ALL_FIELDS), labeller)):
        out[r["item_id"]][r["field"]] = {"values": r["values"], "status": r["status"]}
    return dict(out)


def combined_labels(conn: psycopg.Connection, set_id: int, *, primary: str = OWNER,
                    fallback: str = "claude") -> tuple[dict[int, dict[str, dict]], dict[str, Counter]]:
    """The combined reference for compare(): per item and field, the owner's answer where they gave one
    (sure, not sure or skipped), otherwise the fallback labeller's. Returns the labels and, per
    field, how many came from whom."""
    mine, theirs = gold_labels(conn, set_id, primary), gold_labels(conn, set_id, fallback)
    out: dict[int, dict] = {}
    source: dict[str, Counter] = {f: Counter() for f in ALL_FIELDS}
    for item_id in set(mine) | set(theirs):
        row = {}
        for f in ALL_FIELDS:
            lab, who = (mine.get(item_id) or {}).get(f), primary
            if lab is None:
                lab, who = (theirs.get(item_id) or {}).get(f), fallback
            if lab is not None:
                row[f] = lab
                source[f][who] += 1
        out[item_id] = row
    return out, source


def predictions_from_run(conn: psycopg.Connection, set_id: int, run_id: str) -> dict[int, dict[str, Prediction]]:
    """A model run's proposals (talos.cases) as predictions for compare(): the values the run
    gave the item's message, or else its thread (a thread or window case). Many-value fields
    gather their values; the confidence is the lowest of them."""
    rows = conn.execute(
        """
        select i.id as item_id, a.dimension_id as field, a.value, a.confidence, a.entity_id = i.message_id as own
        from gold_item i join message m on m.id = i.message_id
        join assignment a on a.entity_id in (m.id, m.thread_id) and a.source_kind = 'model' and a.source_ref = %s
                          and a.status in ('proposed', 'active') and a.value not like 'hold:%%'
        where i.set_id = %s order by i.id, a.dimension_id, own desc, a.id
        """, (run_id, set_id)).fetchall()
    grouped: dict[tuple, list] = defaultdict(list)
    for r in rows:
        grouped[r["item_id"], r["field"]].append(r)
    out: dict[int, dict] = defaultdict(dict)
    for (item_id, field), rs in grouped.items():
        own = [r for r in rs if r["own"]] or rs
        if field not in MANY_FIELDS:
            own = own[:1]
        confs = [r["confidence"] for r in own if r["confidence"] is not None]
        out[item_id][field] = Prediction(tuple(dict.fromkeys(r["value"] for r in own)),
                                         min(confs) if confs else None, run_id)
    return dict(out)


def report(conn: psycopg.Connection, set_id: int | None = None, *, run_id: str | None = None,
           labeller: str = OWNER) -> dict:
    """Labels per field, and the rules' and the pre-pass's agreement with them for origin, type
    and topic (and a model run's, with run_id). The reference is the owner's labels, or
    another labeller's (labeller='claude'). Where the owner and another labeller answered the same items, their
    agreement too."""
    if set_id is None:
        row = conn.execute("select max(id) as id from gold_set").fetchone()
        if not row["id"]:
            raise GoldError("there is no answer key yet; make one with: talos enrich gold sample")
        set_id = row["id"]
    labeller = _labeller(labeller)
    prog = progress(conn, set_id, labeller)
    fields = prog["label_fields"]
    gold = gold_labels(conn, set_id, labeller)
    rule_fields = tuple(f for f in RULE_FIELDS if f in fields)
    out = {"set": prog["set"], "labeller": labeller, "fields_labelled": fields, "total": prog["total"],
           "done": prog["done"],
           "sessions": prog["session"]["count"], "fields": prog["fields"],
           "after_reveal": conn.execute("select count(*)::int as n from gold_label where set_id = %s and after_reveal"
                                        " and labeller = %s", (set_id, labeller)).fetchone()["n"],
           "notes": conn.execute("select count(*)::int as n from gold_label where set_id = %s and field = 'note'"
                                 " and labeller = %s", (set_id, labeller)).fetchone()["n"],
           "rules": compare(gold, rule_predictions(conn, set_id, kind="kind" in fields), fields=rule_fields)}
    if run_id:
        out["run"] = {"id": run_id, "scores": compare(gold, predictions_from_run(conn, set_id, run_id), fields=fields)}
    positions = {r["id"]: r["position"] for r in conn.execute(
        "select id, position from gold_item where set_id = %s", (set_id,))}
    for scores in [out["rules"]] + ([out["run"]["scores"]] if run_id else []):
        for res in scores.values():
            for s in res["by_source"].values():
                s["wrong_items"] = sorted(positions.get(i, i) for i in s["wrong_items"])
    out["agreement"] = labeller_agreement(conn, set_id, positions=positions)
    out["labellers"] = multi_labeller(conn, set_id, reference=labeller)
    out["groups"] = group_sizes(conn, set_id, labeller=labeller)
    return out


def pct(x: float | None) -> str:
    """A share as the reports print it: one decimal, or a dash for none."""
    return "—" if x is None else f"{100 * x:.1f}%"


def format_report(rep: dict) -> str:
    s = rep["set"]
    lines = [f"Answer key {s['id']} ({s['name']}, seed {s['seed']}): {rep['done']} of {rep['total']} items done,"
             f" {rep['sessions']} {'session' if rep['sessions'] == 1 else 'sessions'}"
             + (f"; {rep['after_reveal']} answers changed after their round was revealed" if rep["after_reveal"] else "")
             + (f"; {rep['notes']} notes" if rep["notes"] else ""),
             "", "Labels per field (sure / not sure / skipped):"]
    for f, c in rep["fields"].items():
        lines.append(f"  {f:<7} {c['set']:>4} / {c['unsure']:>3} / {c['skip']:>3}")

    def scored(title: str, scores: dict) -> None:
        lines.extend(["", title])
        for f, r in scores.items():
            lines.append(f"  {f}: {r['predicted']} of {r['labelled']} labelled items have a value"
                         f" ({pct(r['coverage'])}); {r['correct']} right ({pct(r['accuracy'])})")
            if not r["predicted"]:
                continue
            if f in MANY_FIELDS:
                lines.append("    any: precision " + pct(r["any"]["precision"]) + ", recall " + pct(r["any"]["recall"]))
                vals = [f"{v} P {pct(d['precision'])} R {pct(d['recall'])}" for v, d in r["per_value"].items()]
            else:
                vals = [f"{v} {d['correct']}/{d['predicted']} ({pct(d['precision'])})"
                        for v, d in r["per_value"].items() if d["predicted"]]
            lines.append("    precision per value: " + ", ".join(vals))
            for src, d in r["by_source"].items():
                wrong = ", ".join(f"{k} {n}" for k, n in d["wrong"].items())
                lines.append(f"    {src}: {d['correct']}/{d['n']} ({pct(d['precision'])})"
                             + (f"; wrong: {wrong} (items {', '.join(map(str, d['wrong_items'][:12]))}"
                                f"{' …' if len(d['wrong_items']) > 12 else ''})" if wrong else ""))
            if len(r["by_bucket"]) > 1:
                lines.append("    by confidence: " + ", ".join(f"{b} {d['correct']}/{d['n']} ({pct(d['precision'])})"
                                                            for b, d in r["by_bucket"].items()))

    who = rep.get("labeller", OWNER)
    whose = "your labels" if who == OWNER else f"{who}'s labels"
    if who != OWNER:
        lines[0] = lines[0].replace(" items done", f" items labelled by {who}")
    scored(f"Rules and the pre-pass against {whose}:", rep["rules"])
    if rep.get("run"):
        scored(f"Model run {rep['run']['id']} against {whose}:", rep["run"]["scores"])
    lines.extend(format_agreement(rep.get("agreement") or []))
    lines.extend(format_multi(rep.get("labellers")))
    lines.extend(format_groups(rep.get("groups")))
    return "\n".join(lines)


# ---------------------------------------------------------------- another labeller: import, agreement, the check
#
# Claude labels every item (labeller 'claude', imported from JSONL); the owner checks a frozen
# random sample of them on the check screen, field by field: they agree (Claude's value becomes
# their answer) or correct it. Nothing here is shown on the blind screen: item() and progress()
# read only the owner's labels.

IMPORT_KEYS = {"position", "unsure", "note"}         # and the set's fields
CHECK_N = 25


def _show(values) -> str:
    return ",".join(sorted(values)) or "none"


def jaccard(field: str, a, b) -> float:
    """The overlap of two answers' sets: shared / all; two empty answers ("none") overlap fully."""
    x, y = {_norm(field, v) for v in a}, {_norm(field, v) for v in b}
    return 1.0 if not x and not y else round(len(x & y) / len(x | y), 4)


def agreement(a: dict[int, dict[str, dict]], b: dict[int, dict[str, dict]], fields=FIELDS) -> dict:
    """How two labellers agree, per field, on the items both answered (gold_labels() shapes).

    A pair where both are sure ('set') is scored: exact is the same value (one-value fields) or
    the same set (ask and route; both none counts); ask and route also get the mean Jaccard
    overlap, so a set that is one value off still earns part. A pair where either is not sure or
    skipped is not scored but counted apart: unsure and skip per side, and for the not-sure pairs
    whether the values matched anyway. The disagreements come as "a→b" with their items."""
    per: dict[str, dict] = {}
    both = sorted(set(a) & set(b))
    for field in fields:
        many = field in MANY_FIELDS
        r = {"items": 0, "compared": 0, "exact": 0, "rate": None, "a_unsure": 0, "b_unsure": 0, "a_skip": 0,
             "b_skip": 0, "unsure_pairs": 0, "unsure_match": 0}
        changes, changed_items, jac = Counter(), [], []
        for item_id in both:
            x, y = a[item_id].get(field), b[item_id].get(field)
            if not x or not y:
                continue
            r["items"] += 1
            for side, lab in (("a", x), ("b", y)):
                r[f"{side}_unsure"] += lab["status"] == "unsure"
                r[f"{side}_skip"] += lab["status"] == "skip"
            if "skip" in (x["status"], y["status"]):
                continue
            same = {_norm(field, v) for v in x["values"]} == {_norm(field, v) for v in y["values"]}
            if x["status"] != "set" or y["status"] != "set":
                r["unsure_pairs"] += 1
                r["unsure_match"] += same
                continue
            r["compared"] += 1
            r["exact"] += same
            if many:
                jac.append(jaccard(field, x["values"], y["values"]))
            if not same:
                changes[f"{_show(x['values'])}→{_show(y['values'])}"] += 1
                changed_items.append(item_id)
        r["rate"] = _rate(r["exact"], r["compared"])
        if many:
            r["jaccard"] = round(sum(jac) / len(jac), 4) if jac else None
        r["changes"] = dict(changes.most_common())
        r["changed_items"] = changed_items
        per[field] = r
    compared = sum(r["compared"] for r in per.values())
    exact = sum(r["exact"] for r in per.values())
    return {"items": len(both), "fields": per, "overall": {"compared": compared, "exact": exact,
                                                             "rate": _rate(exact, compared)}}


def labellers(conn: psycopg.Connection, set_id: int) -> list[str]:
    """The labellers other than the owner with answers in the set."""
    return [r["labeller"] for r in conn.execute(
        "select distinct labeller from gold_label where set_id = %s and labeller <> %s order by 1", (set_id, OWNER))]


def labeller_agreement(conn: psycopg.Connection, set_id: int, *, positions: dict[int, int] | None = None) -> list[dict]:
    """Each other labeller against the owner, on the items both answered: all of them, the ones
    the owner labelled blind, and the ones they checked (where they saw the other's answer first)."""
    if positions is None:
        positions = {r["id"]: r["position"] for r in conn.execute(
            "select id, position from gold_item where set_id = %s", (set_id,))}
    mine = gold_labels(conn, set_id, OWNER)
    fields = set_fields(conn, set_id)
    out = []
    for who in labellers(conn, set_id):
        theirs = gold_labels(conn, set_id, who)
        checked = {r["item_id"] for r in conn.execute(
            "select item_id from gold_check_item where set_id = %s and labeller = %s", (set_id, who))}
        parts = {"all": set(theirs), "blind": set(theirs) - checked, "checked": set(theirs) & checked}
        row = {"labeller": who, "items": len(theirs)}
        for name, ids in parts.items():
            ag = agreement({i: theirs[i] for i in ids}, {i: v for i, v in mine.items() if i in ids}, fields=fields)
            for r in ag["fields"].values():
                r["changed_items"] = sorted(positions.get(i, i) for i in r["changed_items"])
            row[name] = ag
        out.append(row)
    return out


def format_agreement(rows: list[dict]) -> list[str]:
    lines = []
    for row in rows:
        who = row["labeller"]
        lines.extend(["", f"Agreement, {who} against your labels ({row['items']} items labelled by {who}):"])
        for name, title in (("all", "all items both labelled"), ("checked", "items you checked (you saw the answer)"),
                            ("blind", "items you labelled blind")):
            ag = row[name]
            if not ag["items"]:
                continue
            o = ag["overall"]
            lines.append(f"  {title}: {ag['items']} items; {o['exact']} of {o['compared']} fields agree"
                         f" ({pct(o['rate'])})")
            if name != "all":
                continue
            for f, r in ag["fields"].items():
                extra = f", overlap {pct(r['jaccard'])}" if r.get("jaccard") is not None else ""
                apart = [f"{who} not sure {r['a_unsure']}" if r["a_unsure"] else "",
                         f"you not sure {r['b_unsure']}" if r["b_unsure"] else "",
                         f"{who} skipped {r['a_skip']}" if r["a_skip"] else "",
                         f"you skipped {r['b_skip']}" if r["b_skip"] else ""]
                apart = [x for x in apart if x]
                lines.append(f"    {f:<7} {r['exact']}/{r['compared']} ({pct(r['rate'])}){extra}"
                             + (f"; {', '.join(apart)}" if apart else ""))
                if r["changes"]:
                    ch = ", ".join(f"{k} {n}" for k, n in list(r["changes"].items())[:8])
                    lines.append(f"            {who}→you: {ch} (items {', '.join(map(str, r['changed_items'][:12]))}"
                                 f"{' …' if len(r['changed_items']) > 12 else ''})")
    return lines


def import_labels(conn: psycopg.Connection, set_id: int, labeller: str, paths: list) -> dict:
    """Another labeller's answers from JSONL files, one item per line:

        {"position": 12, "origin": "marketing", "type": "promotion", "topic": "Shopping", "ask": [],
         "value": "noise", "route": [], "unsure": ["topic"], "note": "…"}

    Every one of the set's fields is required (the six by default; a set with label_fields names
    its own, kind among them), and no other; one listed in unsure is saved 'unsure' (a one-value
    field may then be null), the rest 'set'; an empty ask or route is "none". Every value is checked against its
    field's list, as on the screen. One bad line refuses the whole import (the GoldError lists
    every problem) and nothing is written. Re-importing an item replaces that labeller's answers
    for it, so the import is idempotent. The owner's own answers are never touched."""
    labeller = _labeller(labeller)
    if labeller == OWNER:
        raise GoldError(f"the import is for another labeller's answers; {OWNER}'s come from the screens")
    if not conn.execute("select 1 from gold_set where id = %s", (set_id,)).fetchone():
        raise GoldError(f"no answer key {set_id}")
    ids = {r["position"]: r["id"] for r in conn.execute(
        "select id, position from gold_item where set_id = %s", (set_id,))}
    fields = set_fields(conn, set_id)
    keys = IMPORT_KEYS | set(fields)
    problems: list[str] = []
    rows: dict[int, list[tuple[str, list[str], str]]] = {}
    seen: dict[int, str] = {}
    lines = 0
    for path in paths:
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"{path}: cannot read ({exc.strerror or exc})")
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            lines += 1
            where = f"{path.name}:{n}"
            bad = lambda msg: problems.append(f"{where}: {msg}")  # noqa: E731
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                bad(f"not JSON ({exc.msg})")
                continue
            if not isinstance(rec, dict):
                bad("a line is one JSON object")
                continue
            pos = rec.get("position")
            if not isinstance(pos, int) or isinstance(pos, bool) or pos not in ids:
                bad(f"position {pos!r} is not an item of answer key {set_id}")
                continue
            if pos in seen:
                bad(f"item {pos} again (first at {seen[pos]})")
                continue
            seen[pos] = where
            extra = sorted(set(rec) - keys)
            if extra:
                bad(f"item {pos}: unknown key{'s' if len(extra) > 1 else ''} {', '.join(extra)}")
            unsure = rec.get("unsure") or []
            if not isinstance(unsure, list) or not all(isinstance(u, str) and u in fields for u in unsure):
                bad(f"item {pos}: unsure is a list of fields ({', '.join(fields)}), not {unsure!r}")
                unsure = []
            out = []
            for field in fields:
                if field not in rec:
                    bad(f"item {pos}: {field} is missing")
                    continue
                v = rec[field]
                status = "unsure" if field in unsure else "set"
                if field in MANY_FIELDS:
                    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                        bad(f"item {pos}: {field} is a list of values (empty for none), not {v!r}")
                        continue
                    values = v
                elif v is None or v == "":
                    values = []
                elif isinstance(v, str):
                    values = [v]
                else:
                    bad(f"item {pos}: {field} is one value, not {v!r}")
                    continue
                try:
                    out.append((field, _check(conn, field, values, status), status))
                except GoldError as exc:
                    bad(f"item {pos}: {exc}")
            note = rec.get("note")
            if note is not None and not isinstance(note, str):
                bad(f"item {pos}: note is a text")
            elif note and note.strip():
                try:
                    out.append(("note", _check(conn, "note", [note], "set"), "set"))
                except GoldError as exc:
                    bad(f"item {pos}: {exc}")
            rows[pos] = out
    if not lines and not problems:
        problems.append("no lines to import")
    if problems:
        shown = problems[:60]
        more = f"\n  … and {len(problems) - len(shown)} more" if len(problems) > len(shown) else ""
        raise GoldError(f"refused, nothing was imported: {len(problems)} "
                        f"{'problem' if len(problems) == 1 else 'problems'}\n  " + "\n  ".join(shown) + more)
    item_ids = [ids[p] for p in rows]
    with conn.transaction():
        replaced = conn.execute("select count(distinct item_id)::int as n from gold_label where labeller = %s"
                                " and item_id = any(%s)", (labeller, item_ids)).fetchone()["n"]
        conn.execute("delete from gold_label where labeller = %s and item_id = any(%s)", (labeller, item_ids))
        with conn.cursor() as cur:
            cur.executemany(
                "insert into gold_label (set_id, item_id, field, values, status, labeller) values (%s, %s, %s, %s, %s, %s)",
                [(set_id, ids[p], f, v, st, labeller) for p, out in rows.items() for f, v, st in out])
    statuses = Counter(st for out in rows.values() for f, _, st in out if f != "note")
    unsure_by = Counter(f for out in rows.values() for f, _, st in out if st == "unsure")
    total = conn.execute("select count(distinct item_id)::int as n from gold_label where set_id = %s"
                         " and labeller = %s and field <> 'note'", (set_id, labeller)).fetchone()["n"]
    return {"set_id": set_id, "labeller": labeller, "lines": lines, "items": len(rows), "replaced": replaced,
            "new": len(rows) - replaced, "fields_set": statuses["set"], "fields_unsure": statuses["unsure"],
            "unsure": dict(unsure_by.most_common()),
            "notes": sum(1 for out in rows.values() for f, _, _ in out if f == "note"),
            "set_items": len(ids), "labelled_items": total}


def _owner_items(conn: psycopg.Connection, set_id: int) -> set[int]:
    """The items the owner has answered at least one field of."""
    return {r["item_id"] for r in conn.execute(
        "select distinct item_id from gold_label_valid where set_id = %s and labeller = %s and field <> 'note'",
        (set_id, OWNER))}


def check_sample(conn: psycopg.Connection, set_id: int, *, n: int = CHECK_N, seed: int | None = None,
                 labeller: str = "claude", replace: bool = False) -> dict:
    """Freeze a random sample of n items that the labeller answered, for the owner to check. Items
    they had already answered themselves (labelled blind) are left out; they are compared on their own.
    The same seed on the same labels draws the same items; seed defaults to the set's. A set has
    one check sample; replace draws a new one (the owner's answers stay, but items they have answered since
    are then left out, so the draw differs)."""
    labeller = _labeller(labeller)
    if n < 1:
        raise GoldError("n must be at least 1")
    s = conn.execute("select id, seed from gold_set where id = %s", (set_id,)).fetchone()
    if not s:
        raise GoldError(f"no answer key {set_id}")
    old = conn.execute("select count(*)::int as n, min(seed) as seed from gold_check_item where set_id = %s",
                       (set_id,)).fetchone()
    if old["n"] and not replace:
        raise GoldError(f"answer key {set_id} already has a check sample of {old['n']} (seed {old['seed']});"
                        " draw a new one with --replace")
    seed = s["seed"] if seed is None else seed
    labelled = [r["item_id"] for r in conn.execute(
        "select l.item_id from gold_label_valid l join gold_item i on i.id = l.item_id where l.set_id = %s"
        " and l.labeller = %s and l.field <> 'note' group by l.item_id, i.position order by i.position",
        (set_id, labeller))]
    owned = _owner_items(conn, set_id)
    cands = [i for i in labelled if i not in owned]
    if not cands:
        raise GoldError(f"no item of answer key {set_id} has {labeller}'s labels (and none of yours) to check;"
                        f" import them first: talos enrich gold import-labels --set {set_id} --labeller {labeller} FILE")
    pick = random.Random(seed).sample(cands, min(n, len(cands)))
    with conn.transaction():
        conn.execute("delete from gold_check_item where set_id = %s", (set_id,))
        with conn.cursor() as cur:
            cur.executemany("insert into gold_check_item (set_id, item_id, rank, labeller, seed) values (%s, %s, %s, %s, %s)",
                            [(set_id, i, k, labeller, seed) for k, i in enumerate(pick, 1)])
    positions = {r["id"]: r["position"] for r in conn.execute(
        "select id, position from gold_item where set_id = %s", (set_id,))}
    return {"set_id": set_id, "labeller": labeller, "seed": seed, "n": len(pick), "candidates": len(cands),
            "positions": [positions[i] for i in pick],
            "excluded": sorted(positions[i] for i in labelled if i in owned)}


def check_state(conn: psycopg.Connection, set_id: int) -> dict | None:
    """The set's check sample and how far the owner is: each item's rank, position and their
    answered fields; None when there is no sample."""
    rows = conn.execute(
        "select c.rank, c.labeller, c.seed, i.position,"
        " (select count(*) from gold_label_valid l where l.item_id = c.item_id and l.labeller = %s"
        "  and l.field = any(%s))::int as fields"
        " from gold_check_item c join gold_item i on i.id = c.item_id where c.set_id = %s order by c.rank",
        (OWNER, set_fields(conn, set_id), set_id)).fetchall()
    if not rows:
        return None
    fields = set_fields(conn, set_id)
    items = [{"rank": r["rank"], "position": r["position"], "fields": r["fields"], "checked": r["fields"] == len(fields)}
             for r in rows]
    return {"set_id": set_id, "labeller": rows[0]["labeller"], "seed": rows[0]["seed"], "fields": fields, "total": len(items),
            "checked": sum(i["checked"] for i in items), "next": next((i["rank"] for i in items if not i["checked"]), None),
            "items": items}


def _check_row(conn: psycopg.Connection, set_id: int, rank: int) -> dict:
    row = conn.execute("select c.*, i.position from gold_check_item c join gold_item i on i.id = c.item_id"
                       " where c.set_id = %s and c.rank = %s", (set_id, rank)).fetchone()
    if not row:
        raise GoldError(f"answer key {set_id} has no check item {rank}")
    return row


def check_item(conn: psycopg.Connection, set_id: int, rank: int) -> dict:
    """One item of the check: exactly what the blind screen shows (item()), and beside it the
    other labeller's answers and note. Not blind, by design: the owner is checking them."""
    c = _check_row(conn, set_id, rank)
    out = item(conn, set_id, c["position"])
    theirs = _labels(conn, [c["item_id"]], c["labeller"]).get(c["item_id"], {})
    note = theirs.pop("note", None)
    theirs = {f: v for f, v in theirs.items() if f in out["fields"]}
    total = conn.execute("select count(*)::int as n from gold_check_item where set_id = %s", (set_id,)).fetchone()["n"]
    out.update(rank=rank, check_total=total, theirs={
        "labeller": c["labeller"], "note": (note["values"] or [None])[0] if note else None,
        "labels": {f: {"values": v["values"], "status": v["status"]} for f, v in theirs.items()}})
    return out


def agree(conn: psycopg.Connection, set_id: int, rank: int, fields: list[str], duration_ms: int | None = None) -> dict:
    """The owner agrees with the other labeller on these fields of a check item: that value becomes
    the owner's answer, sure. A field the labeller left without a value (not sure of a one-value
    field) cannot be agreed with; the owner chooses one instead."""
    c = _check_row(conn, set_id, rank)
    own = set_fields(conn, set_id)
    if not isinstance(fields, list) or not fields or not all(f in own for f in fields):
        raise GoldError(f"fields is a list of {', '.join(own)}")
    theirs = _labels(conn, [c["item_id"]], c["labeller"]).get(c["item_id"], {})
    for f in fields:
        t = theirs.get(f)
        if not t:
            raise GoldError(f"{c['labeller']} gave no answer for {f}; choose one")
        if f not in MANY_FIELDS and not t["values"]:
            raise GoldError(f"{c['labeller']} was not sure of {f} and gave no value; choose one")
    res = None
    for f in dict.fromkeys(fields):
        res = save_label(conn, set_id, c["position"], f, list(theirs[f]["values"]), "set", duration_ms)
    return {"labels": res["labels"], "check": check_state(conn, set_id)}


def check_summary(conn: psycopg.Connection, set_id: int) -> dict | None:
    """What the check found: per field how many of the other labeller's answers the owner kept
    and what they changed (from → to, with the item), the overall rate; and apart from the sample,
    the items the owner had labelled blind that the other labeller labelled too."""
    st = check_state(conn, set_id)
    if not st:
        return None
    who = st["labeller"]
    rows = conn.execute("select c.item_id, c.rank, i.position from gold_check_item c join gold_item i on i.id = c.item_id"
                        " where c.set_id = %s order by c.rank", (set_id,)).fetchall()
    ids = [r["item_id"] for r in rows]
    mine, theirs = _labels(conn, ids), _labels(conn, ids, who)
    fields = st["fields"]
    per = {f: {"answered": 0, "agreed": 0, "changed": [], "rate": None} for f in fields}
    for r in rows:
        m, t = mine.get(r["item_id"], {}), theirs.get(r["item_id"], {})
        for f in fields:
            if f not in m:
                continue
            d = per[f]
            d["answered"] += 1
            tv = t.get(f)
            kept = (tv is not None and m[f]["status"] == "set"
                    and {_norm(f, v) for v in m[f]["values"]} == {_norm(f, v) for v in tv["values"]})
            if kept:
                d["agreed"] += 1
            else:
                d["changed"].append({"rank": r["rank"], "position": r["position"],
                                     "from": {"values": tv["values"], "status": tv["status"]} if tv else None,
                                     "to": {"values": m[f]["values"], "status": m[f]["status"]}})
    for d in per.values():
        d["rate"] = _rate(d["agreed"], d["answered"])
    answered = sum(d["answered"] for d in per.values())
    agreed = sum(d["agreed"] for d in per.values())
    blind_ids = [i for i in _owner_items(conn, set_id) if i not in set(ids)]
    blind_theirs = {i: v for i, v in gold_labels(conn, set_id, who).items() if i in blind_ids}
    blind = agreement(blind_theirs, {i: v for i, v in gold_labels(conn, set_id).items() if i in blind_theirs},
                      fields=fields)
    positions = {r["id"]: r["position"] for r in conn.execute(
        "select id, position from gold_item where id = any(%s)", (list(blind_theirs),))}
    return {"labeller": who, "total": st["total"], "checked": st["checked"], "fields": per,
            "overall": {"answered": answered, "agreed": agreed, "rate": _rate(agreed, answered)},
            "blind": {"positions": sorted(positions.values()), "items": blind["items"], "overall": blind["overall"],
                      "fields": {f: {"compared": r["compared"], "exact": r["exact"]} for f, r in blind["fields"].items()}}}


# ---------------------------------------------------------------- several labellers against the owner, per field
#
# On a set such as 3 (Jev unsure, kind asked): every other labeller (claude, haiku), and Jev three
# ways: the backfill's answers for the item's case (item.info.case_id; kind derived from its full
# type scores, and the kind of its top type), and a Jev run on the set that asked kind directly
# (talos.focus --gold). Only the owner's sure answers are the yardstick: a field they were not
# sure of, or skipped, is left out for everyone.

def score_against(ref: dict[int, dict[str, dict]], theirs: dict[int, dict[str, dict]], fields) -> dict:
    """One labeller against the reference, per field: of the items where the reference is sure,
    how many they answered (compared) and how many match (right; ask and route: the same set).
    Their own "not sure" answers count with the value they gave (none: wrong) and are also
    counted apart (unsure); a skip is not compared."""
    per = {}
    for f in fields:
        r = {"sure": 0, "compared": 0, "right": 0, "unsure": 0, "rate": None, "wrong": Counter()}
        for item_id, labels in ref.items():
            g = labels.get(f)
            if not g or g["status"] != "set":
                continue
            r["sure"] += 1
            t = (theirs.get(item_id) or {}).get(f)
            if not t or t["status"] == "skip":
                continue
            r["compared"] += 1
            r["unsure"] += t["status"] == "unsure"
            same = ({_norm(f, v) for v in t["values"]} == {_norm(f, v) for v in g["values"]}
                    if f in MANY_FIELDS else bool(t["values"]) and _same(f, t["values"], g["values"]))
            r["right"] += same
            if not same:
                r["wrong"][f"{_show(t['values'])}→{_show(g['values'])}"] += 1
        r["rate"] = _rate(r["right"], r["compared"])
        r["wrong"] = dict(r["wrong"].most_common())
        per[f] = r
    compared = sum(r["compared"] for r in per.values())
    right = sum(r["right"] for r in per.values())
    return {"fields": per, "overall": {"compared": compared, "right": right, "rate": _rate(right, compared)}}


def jev_case_labels(conn: psycopg.Connection, set_id: int, *, kind: str = "sum") -> dict[int, dict[str, dict]]:
    """The backfill's answers for each item's case (info.case_id), as labels: a one-value field's
    top value, ask and route as routed (their selection; empty is none). kind: 'sum' derives it
    from the full type scores (the kind whose types add up to most, as accept does), 'top' is the
    kind of the top type."""
    from talos import boundary
    ko = kind_of(conn)
    kinds = sorted(set(ko.values()))
    out: dict[int, dict] = {}
    for r in conn.execute(
            "select i.id as item_id, p.field, p.top, p.selected, p.scores from gold_item i"
            " join enrich_prediction p on p.case_id = (i.info ->> 'case_id')::bigint"
            " where i.set_id = %s and not p.fixed", (set_id,)):
        row = out.setdefault(r["item_id"], {})
        if r["field"] in MANY_FIELDS:
            row[r["field"]] = {"values": list(r["selected"] or []), "status": "set"}
        elif r["field"] in FIELDS and r["top"] is not None:
            row[r["field"]] = {"values": [r["top"]], "status": "set"}
        if r["field"] == "type" and ko:
            if kind == "top":
                k = ko.get(r["top"])
            else:
                k = boundary.decide(boundary.side_probabilities(r["scores"], ko, kinds))[0]
            if k:
                row["kind"] = {"values": [k], "status": "set"}
    return out


def jev_run_labels(conn: psycopg.Connection, run_id: str) -> dict[int, dict[str, dict]]:
    """An answer-key run's stored answers (a focused one: its field alone), as labels."""
    out: dict[int, dict] = {}
    for r in conn.execute("select item_id, field, top, selected from jev_prediction where run_id = %s", (run_id,)):
        v = list(r["selected"] or []) if r["field"] in MANY_FIELDS else ([r["top"]] if r["top"] else [])
        out.setdefault(r["item_id"], {})[r["field"]] = {"values": v, "status": "set"}
    return out


def multi_labeller(conn: psycopg.Connection, set_id: int, *, reference: str = OWNER) -> dict:
    """Every other labeller and Jev (the backfill's case answers; a direct kind run on the set, the
    latest) against the owner's labels, per field of the set (score_against)."""
    fields = set_fields(conn, set_id)
    ref = gold_labels(conn, set_id, reference)
    rows = []
    for who in [w for w in labellers(conn, set_id) if w != reference]:
        rows.append({"labeller": who, **score_against(ref, gold_labels(conn, set_id, who), fields)})
    if conn.execute("select 1 from gold_item where set_id = %s and info ? 'case_id' limit 1", (set_id,)).fetchone():
        rows.append({"labeller": "jev (backfill)", **score_against(ref, jev_case_labels(conn, set_id), fields)})
        if "kind" in fields:
            rows.append({"labeller": "jev (top type's kind)",
                         **score_against(ref, jev_case_labels(conn, set_id, kind="top"), ["kind"])})
    direct = conn.execute("select id from model_run where purpose = 'gold-eval' and (params->>'set_id')::int = %s"
                          " and params->'focus'->>'field' = 'kind' order by created_at desc, id desc limit 1",
                          (set_id,)).fetchone()
    if direct and "kind" in fields:
        rows.append({"labeller": f"jev (kind asked directly, {direct['id']})",
                     **score_against(ref, jev_run_labels(conn, direct["id"]), ["kind"])})
    return {"reference": reference, "fields": fields, "items": len(ref), "rows": rows}


def format_multi(m: dict | None) -> list[str]:
    if not m or not m["rows"]:
        return []
    fields = m["fields"]
    lines = ["", f"Against your labels, per field (right / compared; only your sure answers count; {m['items']} items):",
             "  " + f"{'labeller':<44}" + "".join(f"{f:>12}" for f in fields) + f"{'all':>12}"]
    for r in m["rows"]:
        cell = lambda d: f"{d['right']}/{d['compared']} {pct(d['rate']):>6}" if d and d["compared"] else "—"  # noqa: E731
        lines.append("  " + f"{r['labeller'][:44]:<44}" + "".join(f"{cell(r['fields'].get(f)):>12}" for f in fields)
                     + f"{cell(r['overall']):>12}")
    for r in m["rows"]:
        k = r["fields"].get("kind")
        if k and k["wrong"]:
            lines.append(f"  kind, {r['labeller']} → you: " + ", ".join(f"{p} {n}" for p, n in list(k["wrong"].items())[:8]))
    return lines


# ---------------------------------------------------------------- sender groups: sizes, and the owner's answer given to them

def _group_messages(conn: psycopg.Connection, item_id: int) -> list[int]:
    """A group's messages: its frozen backfill cases' members, and (an unlock set) its frozen messages."""
    return [r["id"] for r in conn.execute(
        "select unnest(c.member_ids) as id from gold_group_case g join enrich_case c on c.id = g.case_id"
        " where g.item_id = %(i)s union select message_id from gold_group_message where item_id = %(i)s order by 1",
        {"i": item_id})]


def group_sizes(conn: psycopg.Connection, set_id: int, *, labeller: str = OWNER) -> dict:
    """Per item, what the owner's answer stands for: a sender group's messages (unless ticked mixed)
    or its own case's messages; the total, and the shared senders the set split by system."""
    params = conn.execute("select params from gold_set where id = %s", (set_id,)).fetchone()["params"] or {}
    rows = conn.execute(
        "select i.id, i.position, i.info, i.stratum, coalesce(cardinality(c.member_ids), 1) as case_messages,"
        " exists (select 1 from gold_label l where l.item_id = i.id and l.field = %s and l.labeller = %s) as mixed"
        " from gold_item i left join enrich_case c on c.id = (i.info ->> 'case_id')::bigint"
        " where i.set_id = %s order by i.position", (MIXED, labeller, set_id)).fetchall()
    items = []
    for r in rows:
        g = (r["info"] or {}).get("sender_group")
        items.append({"position": r["position"], "stratum": r["stratum"], "group": bool(g),
                      "sender": g.get("sender", g.get("key")) if g else None, "system": g.get("system") if g else None,
                      "cases": g["cases"] if g else 1, "messages": g["messages"] if g else r["case_messages"],
                      "mixed": r["mixed"],
                      "covers": r["case_messages"] if not g or r["mixed"] else g["messages"]})
    return {"items": items, "groups": sum(1 for i in items if i["group"]),
            "covers": sum(i["covers"] for i in items), "split_senders": params.get("split_senders") or {}}


def format_groups(g: dict | None) -> list[str]:
    if not g or not g["groups"]:
        return []
    lines = ["", f"What your answers stand for: {g['covers']:,} messages over {len(g['items'])} items"
             f" ({g['groups']} sender groups; a group you ticked mixed counts its own case only):"]
    for i in g["items"]:
        what = (f"sender group {i['sender']}" + (f" · {i['system']}" if i["system"] else "")
                + f": {i['cases']} cases, {i['messages']:,} messages" + (" — MIXED, not given to the group" if i["mixed"] else "")
                if i["group"] else f"{i['stratum']} case: {i['messages']:,} messages")
        lines.append(f"  item {i['position']:>3}  {what}")
    split = g.get("split_senders") or {}
    if split:
        lines.append("Shared senders split by system when the set was drawn (heads; groups of 5+ cases):")
        for sender, d in list(split.items())[:12]:
            lines.append(f"  {sender}: {d['cases']} unsure cases, {d['heads']} heads, {d['groups']} groups"
                         f" ({d['grouped_cases']} cases in them)")
    return lines


def source_for(labeller: str) -> tuple[str, str]:
    """(source_kind, source_ref prefix) for answers given to groups: the owner's are their decisions
    ('human', 'gold'); anyone else's (Claude, Haiku) are a model's values, named after the labeller
    ('model', 'claude-gold'), so they never pose as the owner's and never outrank the owner's or a rule's."""
    return ("human", "gold") if labeller == OWNER else ("model", f"{labeller}-gold")


def propagate_groups(conn: psycopg.Connection, set_id: int, *, labeller: str = OWNER, dry_run: bool = True) -> dict:
    """Give the owner's answers for each sender-group item to every message of the group's cases,
    unless it is ticked mixed. For the bulk after the test, not run by any command by default: dry_run (the
    default) counts and rolls back.

    Written as the owner's decisions: source_kind 'human', source_ref 'gold:<set>:<position>', decided_by
    the labeller, evidence {"propagated_from": {"set", "item", "item_id"}, "sender_group", ...}.
    Only the owner's sure answers of the set's fields go (not sure, skipped and none write nothing). A
    message that already has a human value of that field from anywhere else keeps it and gets
    nothing. Rerunning is idempotent: what a changed answer no longer says is superseded."""
    labeller = _labeller(labeller)
    fields = set_fields(conn, set_id)
    items = conn.execute("select id, position, info from gold_item where set_id = %s and info ? 'sender_group'"
                         " order by position", (set_id,)).fetchall()
    labels = _labels(conn, [i["id"] for i in items], labeller)
    kind, prefix = source_for(labeller)
    out = {"set_id": set_id, "labeller": labeller, "dry_run": dry_run, "items": [], "written": 0, "superseded": 0,
           "kept_human": 0, "source_kind": kind}
    with conn.transaction():
        for it in items:
            lab = labels.get(it["id"], {})
            g = it["info"]["sender_group"]
            ref = f"{prefix}:{set_id}:{it['position']}"
            row = {"position": it["position"], "sender": g.get("sender", g.get("key")), "system": g.get("system"),
                   "mixed": MIXED in lab, "messages": 0, "fields": {}}
            want = [] if MIXED in lab else [(f, v) for f in fields if (lab.get(f) or {}).get("status") == "set"
                                            for v in lab[f]["values"]]
            msgs = _group_messages(conn, it["id"])
            row["messages"] = len(msgs)
            ev = Jsonb({"propagated_from": {"set": set_id, "item": it["position"], "item_id": it["id"]},
                        "sender_group": g.get("key"), "labeller": labeller})
            dims = sorted({f for f, _ in want})
            # what the owner's changed (or mixed) answer no longer says
            out["superseded"] += conn.execute(
                "update assignment a set status = 'superseded', decided_at = now()"
                " where a.source_kind = %s and a.source_ref = %s and a.status = 'active'"
                " and not exists (select 1 from unnest(%s::text[], %s::text[]) w(f, v)"
                "                 where w.f = a.dimension_id and w.v = a.value)",
                (kind, ref, [f for f, _ in want], [v for _, v in want])).rowcount
            for f in dims:
                vals = [v for ff, v in want if ff == f]
                kept = conn.execute(
                    "select count(distinct a.entity_id)::int as n from assignment a where a.entity_id = any(%s)"
                    " and a.dimension_id = %s and a.status = 'active' and a.source_kind = 'human'"
                    " and coalesce(a.source_ref, '') <> %s", (msgs, f, ref)).fetchone()["n"]
                n = conn.execute(
                    "insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, status,"
                    " evidence, decided_by, decided_at)"
                    " select m.id, %s, v.value, %s, %s, 'active', %s, %s, now()"
                    " from unnest(%s::bigint[]) m(id) cross join unnest(%s::text[]) v(value)"
                    " where not exists (select 1 from assignment a where a.entity_id = m.id and a.dimension_id = %s"
                    "                   and a.status = 'active'"
                    "                   and ((a.source_kind = 'human' and coalesce(a.source_ref, '') <> %s)"
                    "                        or (a.source_ref = %s and a.value = v.value)))",
                    (f, kind, ref, ev, labeller, msgs, vals, f, ref, ref)).rowcount
                row["fields"][f] = n
                out["written"] += n
                out["kept_human"] += kept
            out["items"].append(row)
        if dry_run:
            raise psycopg.Rollback()
    return out


def format_propagate(res: dict) -> str:
    lines = [("DRY RUN (rolled back): " if res["dry_run"] else "")
             + f"answer key {res['set_id']}: {res['written']:,} values written as {res['labeller']}'s,"
             f" {res['superseded']:,} superseded; {res['kept_human']:,} messages kept a human value of their own"]
    for i in res["items"]:
        what = f"{i['sender']}" + (f" · {i['system']}" if i["system"] else "") + f", {i['messages']:,} messages"
        lines.append(f"  item {i['position']:>3}  {what}: " + ("mixed, nothing given" if i["mixed"] else
                     ", ".join(f"{f} {n:,}" for f, n in i["fields"].items()) or "no sure answer yet"))
    return "\n".join(lines)
