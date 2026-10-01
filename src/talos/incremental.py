"""Incremental enrichment: new mail gets Jev's judgement shortly after it arrives.

docs/enrichment-plan.md §6 (Incremental) and §11. The archive was judged once, in the backfill
runs (talos.backfill) and the focused kind and value runs (talos.focus). `talos enrich jev new`
judges what came after, the same way, so new mail is judged as the archive was:

**What is new** (`plan()`): incoming e-mail and Teams messages (any direction: a window holds the
owner's lines too) with no model proposal (proposed or active) and no answer-key group value, that are not
members of any case of a backfill, focused or incremental run. The owner's own sent mail (direction out or
self) is never judged, as in the backfill. By default only mail received in the last `since_days`
(7) days; `--backlog` takes everything (a newly enabled account's history, say). Machine and person
mail are told apart as the backfill tells them (gold._MACHINE / gold._PERSON).

**Units**, as the backfill makes them, and reuse where an earlier unit has answers:

| New message              | Earlier case                             | What happens                                  |
|--------------------------|------------------------------------------|-----------------------------------------------|
| machine, template T      | T's three samples agree on every field   | reused: the agreed answers, no Jev            |
|                          | T split on a field                       | asked: the new ones as one case, T's samples as context |
|                          | none, T has 3+ machine messages          | asked: a template (three samples: first, middle, last) |
|                          | none, T has 1 or 2                       | asked: a tail (the pattern's messages)        |
| person, thread H         | H's case, H grew by fewer than GROWN (3) | reused: H's answers                           |
|                          | H's case, H grew by GROWN or more        | asked again: the whole thread                 |
|                          | none                                     | asked: the new ones as the thread's case      |
| Teams, window W          | W's case, fewer than GROWN new lines     | reused: W's answers                           |
|                          | otherwise                                | asked: the window                             |
| sender of an applied answer-key group (not mixed) | the owner's answer | the owner's values, as propagate-groups gives them; never asked |

"Grew" counts the thread's messages (any direction) stored after its case was made; a window's
lines not in its case. A reused case is stored like any case (unit kind tail, thread or window,
key `<unit>~<anchor>`), with the answers it copied (the newest per field of the unit's latest
full-set case and the focused runs since; a template: per field its least sure sample's) and
`enrich_case.source`, which its proposals carry as evidence.propagated_from.

**Asked** cases get the full v2 question set (talos.jev), then the focused sender_kind (people or
machine; never a Teams window's), kind and value questions exactly as the focus runs asked them
(talos.focus; each only once the answer key has been asked that question, as focus.run requires).
Up to four runs, in that order, purpose 'enrich-incremental': `jev-incr-<stamp>` (the full set and
the reused cases), `…-sender_kind`, `…-kind`, `…-value`. Where the direct kind stays unsure, accept
brings back the kind worked out from the full set's type (backfill.kind_from_type).
Proposals are written by backfill.propagate(), so the runs chain holds: the newest run's answer
supersedes older ones for the same message and field.

**Accept** with the stored policy: the thresholds and margin last applied (model_run.params.accept,
the newest), or `accept` in TALOS_HOME/enrich.json. The two-level preset accepts the boundaries
(sender_kind, sphere, form, keep) and kind first, so new mail gets them the same way. Runs of
earlier invocations that were not accepted (it failed) are accepted with them. Then the touched
messages are placed again in the structure plan (structure.refresh_messages) and Jobs & fruit's
fruit is refreshed.

**Guards.** A person thread goes first, then Teams, then machine mail (newest first); units are
taken while they fit --max-cases (500 by default, 60,000 with --backlog) and the budget (each case
costed at the measured tokens of the four questions); the rest waits for the next run. A unit
whose case failed MAX_TRIES times is left alone. A dry run sends nothing, reads no key and writes
nothing (a read-only transaction), and prints what is new, why the rest is not, the units, the
reuse and the estimated cost.

**The hook** (`after_sync`): talos sync --then-rules calls it after its other steps. It runs only
when TALOS_HOME/enrich.json says {"enabled": true} (default off), at most every `every_minutes`
(15), within `daily_budget` dollars (0.50) a day (what the hook's own runs spent since local
midnight) and `max_cases` (500). It never raises: a failure is logged, stored as a failed
enrich_tick and reported to Argus (service talos-enrich, a push check-in expecting the next within
the cadence plus one sync). Each invocation is an enrich_tick row.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import psycopg
from psycopg.types.json import Jsonb

from talos import backfill, boundary, focus, gold, jev, policy
from talos.backfill import BackfillError, Case
from talos.enrich import MACHINE_ORIGINS, NOISE_TYPES, PERSON_ORIGINS
from talos.policy import Policy

log = logging.getLogger("talos.incremental")

PURPOSE = backfill.INCREMENTAL_PURPOSE
SLUG = "talos-enrich"
SETTINGS_FILE = "enrich.json"
DEFAULTS = {"enabled": False, "daily_budget": 0.5, "every_minutes": 15, "max_cases": 500, "since_days": 7}
BACKLOG_MAX_CASES = backfill.MAX_CASES
GROWN = 3                       # a thread or window that grew by this many messages is asked again
MAX_TRIES = 3                   # a unit whose case failed this often is left alone
# The focused questions new mail is asked after the full set. sender_kind, people or machine, asked directly:
# the archive got it in a combined run, which settled most sides;
# new mail only derived it from origin, and was decided less often (86% against 96%). Never of a Teams window,
# whose side is fixed (focus.NO_TEAMS).
FOCUS_FIELDS = ("sender_kind", "kind", "value")
STAGE_ORDER = ("person", "teams", "machine")
EMAIL_FIELDS = ("origin", "type", "topic", "value", "ask", "route")   # a complete e-mail answer
TEAMS_FIELDS = ("type", "topic", "value", "ask", "route")             # a window's origin is fixed
# Tokens per case of the focused questions when no focused run has measured them yet (an archive's
# kind and value runs: 1,526 and 1,327 per case).
FOCUS_TOKENS = {"sender_kind": 850, "kind": 1_550, "value": 1_350}
KEEP_TICKS_DAYS = 30
NOT_ANSWERED = "not answered%"  # a case the run stopped before (budget, refused key): not a failure of the unit


class IncrementalError(BackfillError):
    pass


# ---------------------------------------------------------------- settings

def settings(home: Path) -> dict:
    """TALOS_HOME/enrich.json over DEFAULTS. Raises IncrementalError on a file it cannot read."""
    out = dict(DEFAULTS)
    path = Path(home) / SETTINGS_FILE
    if not path.exists():
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IncrementalError(f"{path}: not readable JSON ({type(exc).__name__})") from None
    if not isinstance(data, dict):
        raise IncrementalError(f"{path}: an object is expected")
    out.update(data)
    if not isinstance(out["enabled"], bool):
        raise IncrementalError(f"{path}: enabled is true or false")
    for k, lo in (("daily_budget", 0.0), ("every_minutes", 1), ("max_cases", 1), ("since_days", 1)):
        v = out[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < lo:
            raise IncrementalError(f"{path}: {k} is a number of at least {lo}")
    acc = out.get("accept")
    if acc is not None and not (isinstance(acc, dict) and isinstance(acc.get("thresholds"), dict)):
        raise IncrementalError(f"{path}: accept is {{\"thresholds\": {{field: p}}, \"margin\": m}}")
    return out


# ---------------------------------------------------------------- what is new

def _migrated(conn: psycopg.Connection) -> bool:
    """Whether 022_incremental.sql is applied (a dry run can size a database that is not yet)."""
    return conn.execute("select 1 from information_schema.columns where table_name = 'enrich_case'"
                        " and column_name = 'source'").fetchone() is not None


def _candidates(conn: psycopg.Connection, *, backlog: bool, since_days: int) -> list[dict]:
    """The new messages (module docstring), oldest first."""
    recent = "" if backlog else " and m.received_at >= now() - make_interval(days => %(since)s)"
    return conn.execute(f"""
        with cand as materialized (
            select m.id, m.account_id, m.medium, m.direction, m.thread_id, m.received_at,
                   lower(m.from_address) as sender, m.subject
            from message m
            where ((m.medium = 'email' and m.direction = 'in') or (m.medium <> 'email' and m.thread_id is not null))
              {recent}
              and not exists (select 1 from assignment a where a.entity_id = m.id and a.source_kind = 'model'
                              and a.status in ('proposed', 'active'))
              and not exists (select 1 from assignment a where a.entity_id = m.id and a.source_kind = 'human'
                              and a.source_ref like 'gold:%%'))
        select c.*, coalesce(mp.pattern_key, 'message:' || c.id) as pk
        from cand c left join message_pattern mp on mp.message_id = c.id
        where not exists (select 1 from enrich_case e join model_run r on r.id = e.run_id
                          where e.member_ids @> array[c.id] and e.error is null and r.purpose = any(%(purposes)s))
        order by c.received_at nulls first, c.id""",
                        {"since": since_days, "purposes": list(backfill.PURPOSES)}).fetchall()


def _pools(conn: psycopg.Connection, ids: list[int]) -> dict[int, str]:
    """{message: 'machine' | 'person'} as gold._MACHINE / gold._PERSON tell them, for these messages
    only (the origin CTE of gold, restricted to them)."""
    if not ids:
        return {}
    rows = conn.execute(f"""
        with ids as (select unnest(%(ids)s::bigint[]) as id),
        o as (
            select distinct on (x.message_id) x.message_id, x.value from (
                select a.entity_id as message_id, a.value, a.source_kind, a.created_at, a.id, true as own
                from ids join assignment a on a.entity_id = ids.id
                where a.dimension_id = 'origin' and a.status = 'active'
                union all
                select m.id, a.value, a.source_kind, a.created_at, a.id, false
                from ids join message m on m.id = ids.id join assignment a on a.entity_id = m.thread_id
                where a.dimension_id = 'origin' and a.status = 'active') x
            order by x.message_id,
                     case x.source_kind when 'human' then 4 when 'rule' then 3 when 'model' then 2 else 1 end desc,
                     x.own desc, x.created_at desc, x.id desc),
        ev as (select distinct e.message_id from event e join ids on ids.id = e.message_id),
        noise as (select distinct a.entity_id as message_id from ids join assignment a on a.entity_id = ids.id
                  where a.dimension_id = 'type' and a.status = 'active' and a.value = any(%(noise)s))
        select m.id, case when {gold._MACHINE} then 'machine' when {gold._PERSON} then 'person' end as pool
        from ids join message m on m.id = ids.id {gold._JOINS}""",
                        {"ids": list(ids), "noise": list(NOISE_TYPES), "machine": list(MACHINE_ORIGINS),
                         "person": list(PERSON_ORIGINS)}).fetchall()
    return {r["id"]: r["pool"] for r in rows}


# ---------------------------------------------------------------- earlier cases

@dataclass
class Source:
    """A unit's latest full-set case(s), with the newest answer per field since (focused runs)."""
    key: str
    run_id: str
    run_at: datetime
    made_at: datetime
    cases: dict[int, dict]                     # sample → case row
    preds: dict[int, dict[str, dict]] = field(default_factory=dict)   # sample → field → prediction

    @property
    def members(self) -> set[int]:
        return {m for c in self.cases.values() for m in c["member_ids"]}

    @property
    def anchors(self) -> list[int]:
        return [self.cases[s]["anchor_id"] for s in sorted(self.cases)]


PRED_COLS = ("field", "top", "selected", "scores", "top3", "confidence", "margin", "decided", "gate", "ungated", "fixed")


def _sources(conn: psycopg.Connection, keys: list[str], *, migrated: bool = True) -> dict[str, Source]:
    """For each unit key, its source: the newest full-set run that has a case of it (a backfill run,
    or an incremental run's full set), and the newest answer per sample and field among that run
    and the runs after it (the focused runs re-ask the same unit and sample)."""
    if not keys:
        return {}
    asked = " and c.source is null" if migrated else ""
    rows = conn.execute(
        "select c.id, c.run_id, r.created_at as run_at, c.unit_key, c.unit_kind, c.sample_no, c.anchor_id,"
        " c.member_ids, c.created_at,"
        " (r.purpose = %(bf)s or (r.purpose = %(inc)s and r.params->'focus' is null)) as full_set"
        " from enrich_case c join model_run r on r.id = c.run_id"
        f" where c.unit_key = any(%(keys)s) and c.error is null{asked} and r.purpose = any(%(purposes)s)",
        {"keys": list(keys), "bf": backfill.PURPOSE, "inc": PURPOSE, "purposes": list(backfill.PURPOSES)}).fetchall()
    by_key: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_key[r["unit_key"]].append(r)
    out: dict[str, Source] = {}
    later_ids: dict[int, tuple] = {}
    for key, rs in by_key.items():
        full = [r for r in rs if r["full_set"]]
        if not full:
            continue
        top = max(full, key=lambda r: (r["run_at"], r["run_id"]))
        mine = {r["sample_no"]: r for r in full if r["run_id"] == top["run_id"]}
        out[key] = Source(key, top["run_id"], top["run_at"], max(r["created_at"] for r in mine.values()), mine)
        for r in rs:
            if (r["run_at"], r["run_id"]) >= (top["run_at"], top["run_id"]):
                later_ids[r["id"]] = (key, r["sample_no"], r["run_at"], r["run_id"])
    if later_ids:
        best: dict[tuple, tuple] = {}
        for p in conn.execute(f"select case_id, {', '.join(PRED_COLS)} from enrich_prediction where case_id = any(%s)",
                              (list(later_ids),)):
            key, sample, run_at, run_id = later_ids[p["case_id"]]
            k = (key, sample, p["field"])
            if k not in best or (run_at, run_id) > best[k][0]:
                best[k] = ((run_at, run_id), p)
        for (key, sample, f), (_, p) in best.items():
            out[key].preds.setdefault(sample, {})[f] = p
    return out


def _sig(f: str, p: dict) -> str:
    if f in gold.MANY_FIELDS:
        return ",".join(sorted(p["selected"] or ()))
    return p["top"] or ""


def _derived_tops(sd: dict | None, preds: dict[str, dict]) -> dict[str, str | None]:
    """The boundaries and kind of one sample, as propagate derives them (a stored one counts)."""
    out = {}
    for b, d in (sd or {}).items():
        if b in preds:
            continue
        src = preds.get(d["from"])
        if src is None:
            continue
        out[b] = boundary.decide(boundary.side_probabilities(src["scores"], d["side_of"], d["sides"]))[0]
    return out


def _complete(preds: dict[str, dict], needed: tuple) -> bool:
    return all(f in preds for f in needed)


def _agreed(src: Source, sd: dict | None) -> list[dict] | None:
    """A template whose three samples agree on every field (and on each derived boundary and kind):
    per field its least sure sample's answer, as backfill.propagate gives a template's other
    messages. None when a sample is missing, incomplete or any field splits."""
    if sorted(src.cases) != [0, 1, 2] or sorted(src.preds) != [0, 1, 2]:
        return None
    ps = [src.preds[s] for s in (0, 1, 2)]
    if not all(_complete(p, EMAIL_FIELDS) for p in ps):
        return None
    fields = set().union(*(p.keys() for p in ps))
    out = []
    for f in sorted(fields):
        answers = [p.get(f) for p in ps]
        if any(a is None for a in answers):
            return None
        if any(a["fixed"] for a in answers):
            out.append(answers[0])
            continue
        if len({_sig(f, a) for a in answers}) != 1:
            return None
        weakest = min(range(3), key=lambda i: (answers[i]["confidence"] if answers[i]["confidence"] is not None
                                               else -1.0, i))
        out.append(answers[weakest])
    derived = [_derived_tops(sd, p) for p in ps]
    for b in set().union(*derived):
        if len({d.get(b) for d in derived}) != 1:
            return None
    return out


# ---------------------------------------------------------------- the plan

@dataclass
class Reuse:
    """A case Jev is not asked: earlier answers given to new messages."""
    stage: str
    kind: str          # tail (a template's) | thread | window
    key: str
    anchor: int
    members: list[int]
    source: dict
    preds: list[dict]


@dataclass
class Unit:
    """What is asked: one unit's case(s)."""
    stage: str
    key: str
    why: str
    new: list[int]
    cases: list[Case]
    latest: datetime | None

    @property
    def messages(self) -> set[int]:
        return {m for c in self.cases for m in c.members}


@dataclass
class Plan:
    candidates: list[dict]
    reuse: list[Reuse] = field(default_factory=list)
    units: list[Unit] = field(default_factory=list)
    groups: list[tuple[dict, list[int]]] = field(default_factory=list)
    given_up: list[str] = field(default_factory=list)

    def counts(self) -> dict:
        by: dict = defaultdict(lambda: defaultdict(int))
        for c in self.candidates:
            stage = "teams" if c["medium"] != "email" else (c.get("pool") or "unknown")
            by[c["account_id"]][stage] += 1
        return {a: dict(v) for a, v in sorted(by.items())}


def _source_json(kind: str, src: Source, preds: list[dict], **extra) -> dict:
    return {"kind": kind, "unit": src.key, "run": src.run_id,
            "cases": sorted({p["case_id"] for p in preds if p.get("case_id")}), **extra}


def _order(conn: psycopg.Connection, ids) -> dict[int, tuple]:
    """(received_at, id) sort keys for messages, received_at nulls first as the backfill orders."""
    rows = conn.execute("select id, received_at, direction from message where id = any(%s)", (list(ids),)).fetchall()
    return {r["id"]: (r["received_at"] is not None, r["received_at"] or datetime.min.replace(tzinfo=timezone.utc),
                      r["id"]) for r in rows}


def _pattern_members(conn: psycopg.Connection, pks: list[str]) -> dict[str, list[int]]:
    """The machine mail of each pattern, oldest first (the backfill's template membership)."""
    pks = [p for p in pks if not p.startswith("message:")]
    if not pks:
        return {}
    rows = conn.execute("select mp.pattern_key as pk, m.id from message_pattern mp join message m on m.id = mp.message_id"
                        " where mp.pattern_key = any(%s) and m.medium = 'email' and m.direction = 'in'"
                        " order by m.received_at nulls first, m.id", (pks,)).fetchall()
    pools = _pools(conn, [r["id"] for r in rows])
    out: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        if pools.get(r["id"]) == "machine":
            out[r["pk"]].append(r["id"])
    return out


def _groups(conn: psycopg.Connection) -> list[dict]:
    """Answer-key sender groups whose answers the owner has given to their messages (propagate-groups
    --apply: the owner's rows 'gold:<set>:<position>' exist) and did not tick mixed; newest set first."""
    items = conn.execute(
        "select i.id, i.set_id, i.position, i.info->'sender_group' as g from gold_item i"
        " where i.info ? 'sender_group'"
        " and exists (select 1 from assignment a where a.source_kind = 'human' and a.status = 'active'"
        "             and a.source_ref = 'gold:' || i.set_id || ':' || i.position)"
        " and not exists (select 1 from gold_label l where l.item_id = i.id and l.field = %s and l.labeller = %s)"
        " order by i.set_id desc, i.position", (gold.MIXED, gold.OWNER)).fetchall()
    if not items:
        return []
    labels = gold._labels(conn, [i["id"] for i in items], gold.OWNER)
    out = []
    for i in items:
        lab = labels.get(i["id"], {})
        want = [(f, v) for f in gold.set_fields(conn, i["set_id"]) if (lab.get(f) or {}).get("status") == "set"
                for v in lab[f]["values"]]
        if want:
            out.append({**i, "want": want})
    return out


def _group_of(groups: list[dict], c: dict) -> dict | None:
    for g in groups:
        s = g["g"] or {}
        if c["sender"] and c["sender"] == (s.get("sender") or "").lower() and (
                not s.get("system") or gold.system_key(c["subject"]) == s["system"]):
            return g
    return None


def _grown(conn: psycopg.Connection, since: dict[int, datetime]) -> dict[int, int]:
    """Per thread: its e-mail messages stored after the given time."""
    if not since:
        return {}
    tids = list(since)
    rows = conn.execute("select s.tid, count(m.id)::int as n from unnest(%s::bigint[], %s::timestamptz[]) s(tid, at)"
                        " left join message m on m.thread_id = s.tid and m.medium = 'email' and m.ingested_at > s.at"
                        " group by 1", (tids, [since[t] for t in tids])).fetchall()
    return {r["tid"]: r["n"] for r in rows}


def _plan_machine(conn, rows: list[dict], group_ids: set[int], sd, migrated: bool) -> tuple[list, list]:
    by_pk: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_pk[r["pk"]].append(r)
    srcs = _sources(conn, [f"template:{pk}" for pk in by_pk], migrated=migrated)
    reuse, units, fresh = [], [], []
    for pk, rs in by_pk.items():
        new = [r["id"] for r in rs]
        plain = [i for i in new if i not in group_ids]
        latest = max((r["received_at"] for r in rs if r["received_at"]), default=None)
        src = srcs.get(f"template:{pk}")
        agreed = _agreed(src, sd) if src else None
        if agreed:
            reuse.append(Reuse("machine", "tail", f"tail:{pk}~{new[-1]}", new[-1], new,
                               _source_json("template", src, agreed), agreed))
        elif not plain:
            continue  # the owner's answer for the sender group stands; nothing is asked
        elif src:
            ctx = [a for a in src.anchors if a != plain[-1]][:3]
            units.append(Unit("machine", f"template:{pk}", "template split", plain,
                              [Case("machine", "tail", f"tail:{pk}~{plain[-1]}", plain[-1], plain, 0,
                                    {"kind": "pattern", "context_ids": ctx, "pattern_key": pk}, len(plain))], latest))
        else:
            fresh.append((pk, plain, latest))
    members = _pattern_members(conn, [pk for pk, _, _ in fresh])
    order = _order(conn, {i for pk, plain, _ in fresh for i in plain + members.get(pk, [])})
    for pk, plain, latest in fresh:
        allm = sorted(set(members.get(pk, [])) | set(plain), key=lambda i: order[i])
        n = len(allm)
        if n >= 3:  # a template: three samples over the pattern; it stands for the new ones
            samples = [allm[0], allm[(n + 1) // 2 - 1], allm[-1]]
            mem = sorted(set(plain) | set(samples), key=lambda i: order[i])
            cases = [Case("machine", "template", f"template:{pk}", a, mem, s,
                          {"kind": "pattern", "context_ids": [i for i in samples if i != a], "pattern_key": pk}, n)
                     for s, a in enumerate(samples)]
            units.append(Unit("machine", f"template:{pk}", "new template", plain, cases, latest))
        else:
            anchor = allm[-1]
            load = ({"kind": "pattern", "context_ids": [i for i in allm if i != anchor], "pattern_key": pk}
                    if n == 2 else {"kind": "message"})
            units.append(Unit("machine", f"tail:{pk}", "tail", plain,
                              [Case("machine", "tail", f"tail:{pk}", anchor, allm, 0, load, n)], latest))
    return reuse, units


def _plan_person(conn, rows: list[dict], migrated: bool) -> tuple[list, list]:
    by_key: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_key[f"thread:{r['thread_id']}" if r["thread_id"] else f"message:{r['id']}"].append(r)
    srcs = _sources(conn, [k for k in by_key if k.startswith("thread:")], migrated=migrated)
    grown = _grown(conn, {int(k.split(":")[1]): s.made_at for k, s in srcs.items()})
    tids = [int(k.split(":")[1]) for k in by_key if k.startswith("thread:")]
    sizes = {r["id"]: r["message_count"] for r in conn.execute(
        "select id, message_count from thread where id = any(%s)", (tids,))} if tids else {}
    reuse, units = [], []
    asks = []
    for key, rs in by_key.items():
        new = [r["id"] for r in rs]
        latest = max((r["received_at"] for r in rs if r["received_at"]), default=None)
        src = srcs.get(key)
        tid = int(key.split(":")[1]) if key.startswith("thread:") else None
        if src and grown.get(tid, 0) < GROWN and 0 in src.preds and _complete(src.preds[0], EMAIL_FIELDS):
            preds = list(src.preds[0].values())
            reuse.append(Reuse("person", "thread", f"{key}~{new[-1]}", new[-1], new,
                               _source_json("thread", src, preds, grown=grown.get(tid, 0)), preds))
            continue
        asks.append((key, tid, set(new), new, latest, "grown" if src else "new thread"))
    # A thread asked again is asked whole, as the backfill asks it: its person-side incoming mail
    # (the new messages, those its earlier cases stood for and those given its answers since).
    again = [a[1] for a in asks if a[5] == "grown"]
    if again:
        rows = conn.execute("select id, thread_id from message where thread_id = any(%s) and medium = 'email'"
                            " and direction = 'in'", (again,)).fetchall()
        pools = _pools(conn, [r["id"] for r in rows])
        whole: dict[int, set[int]] = defaultdict(set)
        for r in rows:
            if pools.get(r["id"]) == "person":
                whole[r["thread_id"]].add(r["id"])
        asks = [(k, t, m | whole.get(t, set()) | (srcs[k].members if k in srcs else set()), n, lt, w)
                if w == "grown" else (k, t, m, n, lt, w) for k, t, m, n, lt, w in asks]
    order = _order(conn, {m for a in asks for m in a[2]})
    for key, tid, members, new, latest, why in asks:
        members = sorted(members, key=lambda i: order[i])
        load = {"kind": "thread", "thread_id": tid} if tid and (sizes.get(tid) or 0) > 1 else {"kind": "message"}
        units.append(Unit("person", key, why, new, [Case("person", "thread", key, members[-1], members, 0, load,
                                                         len(members))], latest))
    return reuse, units


def _plan_teams(conn, rows: list[dict], migrated: bool) -> tuple[list, list]:
    by_id = {r["id"]: r for r in rows}
    tids = sorted({r["thread_id"] for r in rows})
    windows = [w for w in backfill._teams(conn, None, threads=tids) if by_id.keys() & set(w.members)] if tids else []
    srcs = _sources(conn, [w.key for w in windows], migrated=migrated)
    reuse, units = [], []
    for w in windows:
        new = [m for m in w.members if m in by_id]
        latest = max((by_id[m]["received_at"] for m in new if by_id[m]["received_at"]), default=None)
        src = srcs.get(w.key)
        grown = len([m for m in w.members if m not in src.members]) if src else None
        if src and grown < GROWN and 0 in src.preds and _complete(src.preds[0], TEAMS_FIELDS):
            incoming = [m for m in new if by_id[m]["direction"] == "in"]
            anchor = (incoming or new)[-1]
            preds = list(src.preds[0].values())
            reuse.append(Reuse("teams", "window", f"{w.key}~{anchor}", anchor, new,
                               _source_json("window", src, preds, grown=grown), preds))
            continue
        units.append(Unit("teams", w.key, "grown" if src else "new window", new, [w], latest))
    return reuse, units


def _given_up(conn: psycopg.Connection, keys: list[str]) -> set[str]:
    if not keys:
        return set()
    return {r["unit_key"] for r in conn.execute(
        "select c.unit_key from enrich_case c join model_run r on r.id = c.run_id where r.purpose = %s"
        " and c.unit_key = any(%s) and c.error is not null and c.error not like %s"
        " group by 1 having count(*) >= %s", (PURPOSE, keys, NOT_ANSWERED, MAX_TRIES))}


def plan(conn: psycopg.Connection, *, backlog: bool = False, since_days: int = DEFAULTS["since_days"]) -> Plan:
    """What is new and what becomes of it (module docstring). Plain SELECTs: works read-only."""
    migrated = _migrated(conn)
    cands = _candidates(conn, backlog=backlog, since_days=since_days)
    email = [c for c in cands if c["medium"] == "email"]
    pools = _pools(conn, [c["id"] for c in email])
    for c in email:
        c["pool"] = pools.get(c["id"])
    p = Plan(cands)
    groups = _groups(conn)
    group_ids: set[int] = set()
    if groups:
        per: dict[int, tuple[dict, list[int]]] = {}
        for c in email:
            g = _group_of(groups, c)
            if g is not None:
                per.setdefault(g["id"], (g, []))[1].append(c["id"])
                group_ids.add(c["id"])
        p.groups = list(per.values())
    try:
        sd = boundary.sides(conn)
    except boundary.BoundaryError:
        sd = None
    r1, u1 = _plan_machine(conn, [c for c in email if c["pool"] == "machine"], group_ids, sd, migrated)
    r2, u2 = _plan_person(conn, [c for c in email if c["pool"] == "person" and c["id"] not in group_ids], migrated)
    r3, u3 = _plan_teams(conn, [c for c in cands if c["medium"] != "email"], migrated)
    p.reuse = r1 + r2 + r3
    units = u1 + u2 + u3
    stuck = _given_up(conn, sorted({c.key for u in units for c in u.cases}))
    p.given_up = sorted(u.key for u in units if any(c.key in stuck for c in u.cases))
    p.units = sorted((u for u in units if not any(c.key in stuck for c in u.cases)),
                     key=lambda u: (STAGE_ORDER.index(u.stage),
                                    -(u.latest.timestamp()) if u.latest else 0, u.key))
    return p


# ---------------------------------------------------------------- cost

def per_case_tokens(conn: psycopg.Connection) -> dict[str, int]:
    """Measured input tokens per case: the full set (the backfill's measure), and each focused
    question from the focused runs so far (else FOCUS_TOKENS)."""
    out = {"full": backfill.MEASURED_TOKENS_PER_CASE, **FOCUS_TOKENS}
    for r in conn.execute(
            "select r.params->'focus'->>'field' as f, avg(c.input_tokens)::float as t from enrich_case c"
            " join model_run r on r.id = c.run_id where r.purpose = any(%s) and r.params->'focus'->>'field' = any(%s)"
            " and c.error is null and c.input_tokens > 0 group by 1",
            ([backfill.FOCUS_PURPOSE, PURPOSE], list(FOCUS_FIELDS))):
        out[r["f"]] = round(r["t"])
    return out


def _trim(units: list[Unit], max_cases: int, budget: float | None, per_case_usd: float) -> tuple[list, list]:
    """Units in order while they fit max_cases and the budget; the rest waits."""
    chosen, deferred, n, cost = [], [], 0, 0.0
    for u in units:
        k = len(u.cases)
        if n + k <= max_cases and (budget is None or cost + k * per_case_usd <= budget + 1e-12):
            chosen.append(u)
            n += k
            cost += k * per_case_usd
        else:
            deferred.append(u)
    return chosen, deferred


def estimate(conn: psycopg.Connection, cases: list[Case], model: str = jev.MODEL) -> dict:
    """Tokens and cost of the four questions for these cases: by the size of an even sample of
    their records (calibrated as the backfill's estimate is), and at the measured tokens per case."""
    dims = jev.taxonomy_from_db(conn)
    qfull = jev.question_sets(dims)
    qfocus = {f: focus.question_sets(focus.dims_for(conn, f), f) for f in FOCUS_FIELDS}
    measured = per_case_tokens(conn)
    m_per = sum(measured[k] for k in ("full", *FOCUS_FIELDS))
    out = {"cases": len(cases), "sampled": 0, "tokens_per_case": {}, "input_tokens": 0, "cost_usd": 0.0,
           "measured_tokens_per_case": measured, "measured_input_tokens": m_per * len(cases),
           "measured_cost_usd": round(jev.cost(m_per * len(cases)), 4),
           "seconds": round(3 * len(cases) / backfill.MEASURED_CASES_PER_SECOND, 1), "example": None}
    if not cases:
        return out
    step = max(1, len(cases) // backfill.ESTIMATE_SAMPLE)
    built = [b for b in backfill._build(conn, cases[::step][:backfill.ESTIMATE_SAMPLE], qfull, model) if b[3] is None]
    if not built:
        return out

    def cal(rec):
        return backfill.CALIBRATION["teams" if rec.get("unit") == "window" else "email"]
    per = {"full": statistics.mean(jev.estimate_tokens(b[2]) * cal(b[1]) for b in built)}
    for f, qs in qfocus.items():
        per[f] = statistics.mean(jev.estimate_tokens(jev.request_body(b[1], jev.questions_for(qs, b[1]), model=model))
                                 * cal(b[1]) for b in built)
    tokens = round(sum(per.values()) * len(cases))
    out.update(sampled=len(built), tokens_per_case={k: round(v) for k, v in per.items()}, input_tokens=tokens,
               cost_usd=round(jev.cost(tokens), 4), example=built[0])
    return out


# ---------------------------------------------------------------- why the rest is not judged

def census(conn: psycopg.Connection, since_days: int = DEFAULTS["since_days"]) -> list[dict]:
    """Per account and medium: all messages, the owner's own (out, self: never judged), those with a model
    value, and the incoming ones without (new in the last since_days days, and older)."""
    return conn.execute("""
        select m.account_id, m.medium, count(*)::int as total,
               count(*) filter (where m.medium = 'email' and m.direction = 'out')::int as sent,
               count(*) filter (where m.medium = 'email' and m.direction = 'self')::int as to_self,
               count(*) filter (where j.judged)::int as judged,
               count(*) filter (where not j.judged and (m.medium <> 'email' or m.direction = 'in'))::int as unjudged,
               count(*) filter (where not j.judged and (m.medium <> 'email' or m.direction = 'in')
                                and m.received_at >= now() - make_interval(days => %s))::int as unjudged_recent
        from message m
        cross join lateral (select exists (select 1 from assignment a where a.entity_id = m.id
                                           and a.source_kind = 'model' and a.status in ('proposed', 'active'))
                                   as judged) j
        group by 1, 2 order by 1, 2""", (since_days,)).fetchall()


# ---------------------------------------------------------------- the run

def stored_policy(conn: psycopg.Connection, override: dict | None = None) -> dict | None:
    """The accept policy to apply: enrich.json's `accept`, else what was last applied to a backfill,
    focused or incremental run (params.accept, the newest). None when nothing was ever accepted."""
    if override:
        return {"thresholds": dict(override["thresholds"]),
                "margin": float(override.get("margin", backfill.ACCEPT_MARGIN)), "from": SETTINGS_FILE}
    r = conn.execute("select id, params->'accept' as a from model_run where purpose = any(%s) and params ? 'accept'"
                     " order by (params->'accept'->>'at')::timestamptz desc nulls last, id desc limit 1",
                     (list(backfill.PURPOSES),)).fetchone()
    if not r or not (r["a"] or {}).get("thresholds"):
        return None
    return {"thresholds": dict(r["a"]["thresholds"]), "margin": float(r["a"].get("margin") or backfill.ACCEPT_MARGIN),
            "from": r["id"], "decided_by": r["a"].get("decided_by")}


def _commit(conn: psycopg.Connection) -> None:
    if not conn.autocommit:
        conn.commit()


def _start_tick(conn: psycopg.Connection, trigger: str, backlog: bool) -> int:
    tid = conn.execute("insert into enrich_tick (trigger, backlog) values (%s, %s) returning id",
                       (trigger, backlog)).fetchone()["id"]
    _commit(conn)
    return tid


def _finish_tick(conn: psycopg.Connection, tick: int, ok: bool, summary: dict) -> None:
    if conn.info.transaction_status == psycopg.pq.TransactionStatus.INERROR:
        conn.rollback()
    conn.execute("update enrich_tick set finished_at = now(), ok = %s, runs = %s, cases = %s, reused = %s,"
                 " cost_usd = %s, summary = %s where id = %s",
                 (ok, summary.get("runs") or [], (summary.get("asked") or {}).get("cases", 0),
                  (summary.get("reused") or {}).get("units", 0), summary.get("cost_usd") or 0.0,
                  Jsonb(json.loads(json.dumps(summary, default=str))), tick))
    conn.execute("delete from enrich_tick where started_at < now() - make_interval(days => %s)", (KEEP_TICKS_DAYS,))
    _commit(conn)


def _store_reuse(conn: psycopg.Connection, run_id: str, reuse: list[Reuse]) -> int:
    with conn.transaction():
        for r in reuse:
            cid = conn.execute(
                "insert into enrich_case (run_id, stage, unit_kind, unit_key, sample_no, anchor_id, member_ids, source)"
                " values (%s, %s, %s, %s, 0, %s, %s, %s) returning id",
                (run_id, r.stage, r.kind, r.key, r.anchor, r.members, Jsonb(r.source))).fetchone()["id"]
            with conn.cursor() as cur:
                cur.executemany(
                    f"insert into enrich_prediction (case_id, {', '.join(PRED_COLS)})"
                    f" values (%s, {', '.join(['%s'] * len(PRED_COLS))})",
                    [(cid, *[Jsonb(p[c]) if c in ("scores", "top3") else p[c] for c in PRED_COLS]) for p in r.preds])
    return len(reuse)


def _give_groups(conn: psycopg.Connection, groups: list[tuple[dict, list[int]]]) -> int:
    """The owner's answer for a sender group, given to its new messages as propagate-groups gives it."""
    n = 0
    with conn.transaction():
        for g, msgs in groups:
            ref = f"gold:{g['set_id']}:{g['position']}"
            ev = Jsonb({"propagated_from": {"set": g["set_id"], "item": g["position"], "item_id": g["id"]},
                        "sender_group": (g["g"] or {}).get("key"), "labeller": gold.OWNER, "incremental": True})
            n += conn.execute(
                "insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, status, evidence,"
                " decided_by, decided_at)"
                " select m.id, w.f, w.v, 'human', %s, 'active', %s, %s, now()"
                " from unnest(%s::bigint[]) m(id) cross join unnest(%s::text[], %s::text[]) w(f, v)"
                " where not exists (select 1 from assignment a where a.entity_id = m.id and a.dimension_id = w.f"
                "                   and a.status = 'active' and a.source_kind = 'human')",
                (ref, ev, gold.OWNER, msgs, [f for f, _ in g["want"]], [v for _, v in g["want"]])).rowcount
    return n


def _count_cases(conn: psycopg.Connection, run_id: str) -> None:
    c = conn.execute("select count(*) filter (where error is null)::int as done, count(*) filter (where error is not null)"
                     "::int as failed from enrich_case where run_id = %s", (run_id,)).fetchone()
    conn.execute("update model_run set output_count = %s, params = params || jsonb_build_object('cases_done', %s::int,"
                 " 'failed', %s::int) where id = %s", (c["done"], c["done"], c["failed"], run_id))
    _commit(conn)


def _unit_summary(units: list[Unit]) -> dict:
    out = {"units": len(units), "cases": sum(len(u.cases) for u in units),
           "new_messages": sum(len(u.new) for u in units), "messages": len(set().union(*(u.messages for u in units)))
           if units else 0, "by_stage": {}, "why": {}}
    for u in units:
        s = out["by_stage"].setdefault(u.stage, {"units": 0, "cases": 0})
        s["units"] += 1
        s["cases"] += len(u.cases)
        out["why"][u.why] = out["why"].get(u.why, 0) + 1
    return out


def _reuse_summary(reuse: list[Reuse]) -> dict:
    out = {"units": len(reuse), "messages": sum(len(r.members) for r in reuse), "by_kind": {}}
    for r in reuse:
        k = r.source["kind"]
        out["by_kind"][k] = out["by_kind"].get(k, 0) + len(r.members)
    return out


def run(conn: psycopg.Connection, *, client: jev.JevClient | None = None, backlog: bool = False,
        since_days: int | None = None, max_cases: int | None = None, budget: float | None = None,
        dry_run: bool = False, trigger: str = "cli", accept_policy: dict | None = None,
        pol: Policy = policy.DEFAULT, chunk: int = backfill.CHUNK,
        progress: Callable[[str], None] = print) -> dict:
    """Judge what is new (module docstring); with dry_run only plan, estimate and explain."""
    since_days = since_days or DEFAULTS["since_days"]
    if max_cases is None:
        max_cases = BACKLOG_MAX_CASES if backlog else DEFAULTS["max_cases"]
    if max_cases < 1:
        raise IncrementalError("--max-cases is at least 1")
    if budget is not None and budget < 0:
        raise IncrementalError("--budget is a number of dollars, at least 0")
    if dry_run:
        idle = conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
        with conn.transaction():
            if idle:
                conn.execute("set transaction read only")
            p = plan(conn, backlog=backlog, since_days=since_days)
            per = per_case_tokens(conn)
            per_usd = jev.cost(sum(per[k] for k in ("full", *FOCUS_FIELDS)))
            chosen, deferred = _trim(p.units, max_cases, budget, per_usd)
            est = estimate(conn, [c for u in chosen for c in u.cases], client.model if client else jev.MODEL)
            cen = census(conn, since_days)
            pol_s = stored_policy(conn, accept_policy)
            gates = {f: bool(focus.gold_runs(conn, f, focus.question_version(
                focus.question_sets(focus.dims_for(conn, f), f)))) for f in FOCUS_FIELDS}
        example = est.pop("example", None)
        return {"dry_run": True, "backlog": backlog, "since_days": since_days, "max_cases": max_cases, "budget": budget,
                "selected": len(p.candidates), "by_account": p.counts(), "census": cen,
                "groups": {"groups": len(p.groups), "messages": sum(len(m) for _, m in p.groups)},
                "reused": _reuse_summary(p.reuse), "asked": _unit_summary(chosen), "deferred": _unit_summary(deferred),
                "given_up": p.given_up, "estimate": est, "focus_gates": gates, "policy": pol_s,
                "request": example[2] if example else None,
                "example_unit": {"key": example[0].key, "anchor": example[0].anchor, "kind": example[0].kind}
                if example else None}
    tick = _start_tick(conn, trigger, backlog)
    try:
        summary = _run(conn, tick, client=client, backlog=backlog, since_days=since_days, max_cases=max_cases,
                       budget=budget, trigger=trigger, accept_policy=accept_policy, pol=pol, chunk=chunk,
                       progress=progress)
    except Exception as exc:
        _finish_tick(conn, tick, False, {"error": f"{type(exc).__name__}: {exc}"})
        raise
    _finish_tick(conn, tick, not summary.get("error"), summary)
    return summary


def _run(conn, tick, *, client, backlog, since_days, max_cases, budget, trigger, accept_policy, pol, chunk,
         progress) -> dict:
    from talos import structure, unlock  # the planner and Jobs & fruit, after the values changed
    p = plan(conn, backlog=backlog, since_days=since_days)
    per = per_case_tokens(conn)
    per_usd = jev.cost(sum(per[k] for k in ("full", *FOCUS_FIELDS)))
    chosen, deferred = _trim(p.units, max_cases, budget, per_usd)
    summary: dict = {"tick": tick, "trigger": trigger, "backlog": backlog, "selected": len(p.candidates),
                     "by_account": p.counts(), "reused": _reuse_summary(p.reuse), "asked": _unit_summary(chosen),
                     "deferred": _unit_summary(deferred), "given_up": p.given_up, "runs": [], "notes": [],
                     "cost_usd": 0.0, "failed": []}
    summary["groups"] = {"groups": len(p.groups), "messages": sum(len(m) for _, m in p.groups),
                         "values": _give_groups(conn, p.groups) if p.groups else 0}
    _commit(conn)
    runs: list[str] = []
    spent = 0.0
    if p.reuse or chosen:
        dims = jev.taxonomy_from_db(conn)
        qsets = jev.question_sets(dims)
        qversion = jev.question_version(qsets)
        model = client.model if client else jev.MODEL
        full_id = f"jev-incr-{jev.now_stamp()}-t{tick}"
        backfill.check_run_id(full_id)
        cases = [c for u in chosen for c in u.cases]
        info = {"part": "full", "tick": tick, "trigger": trigger, "backlog": backlog}
        params = {**backfill._params(None, qversion, pol, client), "incremental": info}
        conn.execute("insert into model_run (id, model, purpose, params, input_count) values (%s, %s, %s, %s, %s)",
                     (full_id, model, PURPOSE, Jsonb(params), len(p.reuse) + len(cases)))
        _commit(conn)
        _store_reuse(conn, full_id, p.reuse)
        _commit(conn)
        progress(f"new: run {full_id}: {len(p.reuse):,} units reused, {len(cases):,} cases to ask"
                 f" ({len(deferred):,} units wait)")
        if cases:
            client = client or jev.JevClient()
            before = client.usage.input_tokens
            state, _ = backfill._send(conn, full_id, "new", cases, client, qsets, dims, pol, model, chunk=chunk,
                                      budget=budget, progress=progress)
            summary["failed"] += [{"unit": k, "error": e} for k, e in state["failed"]]
            if state["stopped"]:
                summary["error"] = state["stopped"]
        else:
            before = 0
            _count_cases(conn, full_id)
        summary["propagation"] = {full_id: backfill.propagate(conn, full_id)}
        _commit(conn)
        runs.append(full_id)
        answered = {(r["unit_key"], r["sample_no"]) for r in conn.execute(
            "select unit_key, sample_no from enrich_case where run_id = %s and error is null and source is null",
            (full_id,))}
        ok_cases = [c for c in cases if (c.key, c.sample) in answered]
        for f in FOCUS_FIELDS:
            if not ok_cases or summary.get("error"):
                break
            dims_f = focus.dims_for(conn, f)
            qsets_f = focus.question_sets(dims_f, f)
            qv_f = focus.question_version(qsets_f)
            if not focus.gold_runs(conn, f, qv_f):
                summary["notes"].append(f"focused {f} not asked: the answer key has not been asked this question"
                                        f" (question version {qv_f}); run talos enrich jev focus --field {f} --gold")
                continue
            spent = jev.cost(client.usage.input_tokens - before)
            rest = None if budget is None else budget - spent
            if rest is not None and rest <= 0:
                summary["notes"].append(f"focused {f} not asked: the budget is spent")
                break
            asked = [c for c in ok_cases if not (f in focus.NO_TEAMS and c.stage == "teams")]
            if not asked:
                continue
            fid = f"{full_id}-{f}"
            fparams = {**focus._params(f, {"where": "new", "stages": sorted({c.stage for c in asked}), "since": None,
                                           "chain": [full_id]}, qv_f, pol, client),
                       "incremental": {**info, "part": f, "of": full_id}}
            conn.execute("insert into model_run (id, model, purpose, params, input_count) values (%s, %s, %s, %s, %s)",
                         (fid, model, PURPOSE, Jsonb(fparams), len(asked)))
            _commit(conn)
            state, _ = backfill._send(conn, fid, f"new {f}", asked, client, qsets_f, dims_f, pol, model,
                                      chunk=chunk, budget=rest, progress=progress)
            summary["failed"] += [{"unit": k, "error": e, "field": f} for k, e in state["failed"]]
            summary["propagation"][fid] = backfill.propagate(conn, fid)
            _commit(conn)
            runs.append(fid)
            if state["stopped"]:
                summary["error"] = state["stopped"]
        if cases:
            spent = jev.cost(client.usage.input_tokens - before)
            summary["usage"] = {"input_tokens": client.usage.input_tokens - before}
    summary["runs"] = runs
    summary["cost_usd"] = round(spent, 6)
    # Accept with the stored policy: these runs, and any earlier incremental run left unaccepted.
    pol_s = stored_policy(conn, accept_policy)
    summary["policy"] = pol_s
    pending = [r["id"] for r in conn.execute(
        "select id from model_run where purpose = %s and not (params ? 'accept') and not (id = any(%s))"
        " order by created_at, id", (PURPOSE, runs))]
    to_accept = pending + runs
    if pol_s and to_accept:
        acc = backfill.accept(conn, to_accept, pol_s["thresholds"], margin=pol_s["margin"])
        _commit(conn)
        summary["accept"] = {"runs": to_accept, "decided_by": acc["decided_by"],
                             "promoted": {f: d["promoted"] for f, d in acc["fields"].items()}}
    elif to_accept:
        summary["notes"].append("nothing accepted: no policy was ever applied (talos enrich accept --run all"
                                " --two-level), and enrich.json has no accept")
    touched = sorted({r["id"] for r in conn.execute(
        "select distinct unnest(member_ids) as id from enrich_case where run_id = any(%s) and error is null",
        (runs,))} | {m for _, ms in p.groups for m in ms}) if (runs or p.groups) else []
    placed = structure.refresh_messages(conn, touched) if touched else None
    summary["placed"] = {k: v["placed"] for k, v in (placed or {}).items()}
    summary["touched"] = len(touched)
    if touched:
        unlock.refresh_fruit(conn)
    _commit(conn)
    log.info("enrich new: %s", format_line(summary))
    return summary


# ---------------------------------------------------------------- the hook

def spent_today(conn: psycopg.Connection, now: datetime | None = None) -> float:
    """What the hook's runs spent since local midnight."""
    now = (now or datetime.now(timezone.utc)).astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return float(conn.execute("select coalesce(sum(cost_usd), 0)::float as s from enrich_tick"
                              " where trigger = 'sync' and started_at >= %s", (midnight,)).fetchone()["s"])


def due(conn: psycopg.Connection, every_minutes: float, now: datetime | None = None) -> bool:
    """Whether the hook's last run is at least every_minutes ago (a minute's slack: the sync that
    comes a few seconds early is not made to wait five more minutes)."""
    last = conn.execute("select max(started_at) as t from enrich_tick where trigger = 'sync'").fetchone()["t"]
    now = now or datetime.now(timezone.utc)
    return last is None or now - last >= timedelta(minutes=every_minutes) - timedelta(seconds=60)


def after_sync(conn: psycopg.Connection, home: Path, dsn: str, *, client: jev.JevClient | None = None,
               now: datetime | None = None) -> dict | None:
    """After talos sync --then-rules: judge what is new, when enrich.json turns it on and it is due.
    Never raises; the result goes to the log and to Argus (talos-enrich)."""
    from talos import argus
    cfg: dict = dict(DEFAULTS)
    try:
        cfg = settings(home)
        if not cfg["enabled"]:
            return None
        if not due(conn, cfg["every_minutes"], now):
            return {"skipped": "not due"}
        rest = max(0.0, float(cfg["daily_budget"]) - spent_today(conn, now))
        res = run(conn, client=client, since_days=int(cfg["since_days"]), max_cases=int(cfg["max_cases"]),
                  budget=rest, trigger="sync", accept_policy=cfg.get("accept"), progress=log.info)
        line = format_line(res) + (" (the daily budget is spent)" if rest <= 0 and res["deferred"]["units"] else "")
        argus.ping(dsn, SLUG, ok=not res.get("error"),
                   expected_next_within=int(cfg["every_minutes"] * 60) + argus.SYNC_EVERY, summary=line)
        return res
    except Exception as exc:  # noqa: BLE001 — a sync is never failed by the enrichment
        try:
            if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                conn.rollback()
            log.exception("enrich new failed; the sync is not affected")
            argus.ping(dsn, SLUG, ok=False, summary=f"failed: {type(exc).__name__}: {exc}"[:200],
                       expected_next_within=int(float(cfg.get("every_minutes") or 15) * 60) + argus.SYNC_EVERY)
        except Exception:  # noqa: BLE001
            pass
        return None


# ---------------------------------------------------------------- output

def format_line(s: dict) -> str:
    a, r, d = s["asked"], s["reused"], s["deferred"]
    return (f"{s['selected']:,} new; {r['messages']:,} messages reused from {r['units']:,} units;"
            f" {a['cases']:,} cases asked ({a['units']:,} units); {d['units']:,} units wait"
            + (f"; {s['groups']['messages']:,} by your group answers" if s.get("groups", {}).get("messages") else "")
            + f"; ${s.get('cost_usd', 0):.4f}" + (f"; error: {s['error']}" if s.get("error") else ""))


def format_status(cfg: dict, conn: psycopg.Connection) -> str:
    ticks = conn.execute("select * from enrich_tick order by started_at desc limit 5").fetchall()
    lines = [f"enrich.json: {json.dumps(cfg)}",
             f"spent today by the hook: ${spent_today(conn):.4f} of ${float(cfg['daily_budget']):.2f}",
             "last runs:"]
    for t in ticks:
        lines.append(f"  {t['started_at']:%Y-%m-%d %H:%M} {t['trigger']:<4} {'ok' if t['ok'] else 'FAILED' if t['ok'] is False else '…'}"
                     f" {t['cases']:,} cases, {t['reused']:,} reused, ${t['cost_usd']:.4f}"
                     + (f" — {t['summary'].get('error')}" if t["summary"].get("error") else ""))
    if not ticks:
        lines.append("  none yet")
    return "\n".join(lines)


def format_run(s: dict) -> str:
    lines = [format_line(s)]
    for r in s["runs"]:
        pr = s.get("propagation", {}).get(r) or {}
        lines.append(f"  run {r}: {pr.get('written', 0):,} proposals written, {pr.get('superseded', 0):,} older ones"
                     " superseded")
    if s.get("accept"):
        lines.append(f"  accepted ({s['accept']['decided_by']}): "
                     + ", ".join(f"{f} {n:,}" for f, n in s["accept"]["promoted"].items()))
    if s.get("placed"):
        lines.append("  placed again: " + ", ".join(f"{k} {n:,}" for k, n in s["placed"].items()))
    for n in s.get("notes") or []:
        lines.append(f"  note: {n}")
    for f in (s.get("failed") or [])[:20]:
        lines.append(f"  failed: {f['unit']}: {f['error']}")
    return "\n".join(lines)


def format_dry_run(s: dict) -> str:
    e = s["estimate"]
    lines = ["DRY RUN: nothing is sent, no key is read, nothing is stored.",
             ("Backlog: every unjudged message" if s["backlog"]
              else f"New mail: unjudged messages received in the last {s['since_days']} days")
             + f"; at most {s['max_cases']:,} cases" + (f", budget ${s['budget']:.2f}" if s["budget"] is not None else ""),
             "", "What is in the archive (per account; your own mail is never judged):",
             f"  {'account':<9} {'medium':<11} {'total':>8} {'sent':>7} {'to self':>7} {'judged':>8} {'unjudged':>9}"
             f" {'of them recent':>14}"]
    for r in s["census"]:
        lines.append(f"  {r['account_id']:<9} {r['medium']:<11} {r['total']:>8,} {r['sent']:>7,} {r['to_self']:>7,}"
                     f" {r['judged']:>8,} {r['unjudged']:>9,} {r['unjudged_recent']:>14,}")
    lines += ["", f"Selected: {s['selected']:,} new messages (not a member of any case, no model value): "
              + "; ".join(f"{a} " + ", ".join(f"{k} {n:,}" for k, n in v.items()) for a, v in s["by_account"].items())]
    r = s["reused"]
    lines.append(f"Reused without Jev: {r['messages']:,} messages in {r['units']:,} units ("
                 + ", ".join(f"{k} {n:,}" for k, n in r["by_kind"].items()) + ")")
    if s["groups"]["messages"]:
        lines.append(f"Your answer-key group answers: {s['groups']['messages']:,} messages in {s['groups']['groups']} groups")
    a = s["asked"]
    lines.append(f"Asked: {a['units']:,} units, {a['cases']:,} cases, for {a['new_messages']:,} new messages"
                 f" (standing for {a['messages']:,}): "
                 + ", ".join(f"{k} {v['units']:,} units/{v['cases']:,} cases" for k, v in a["by_stage"].items())
                 + "; " + ", ".join(f"{k} {n:,}" for k, n in a["why"].items()))
    d = s["deferred"]
    if d["units"]:
        lines.append(f"Waiting (over --max-cases or the budget): {d['units']:,} units, {d['cases']:,} cases")
    if s["given_up"]:
        lines.append(f"Left alone (failed {MAX_TRIES} times): {len(s['given_up'])} units")
    lines += ["", "Each case is asked four questions: the full set, then people or machine (not a Teams window),"
                  " kind and value"
              + ("" if all(s["focus_gates"].values()) else
                 " (NOT asked: " + ", ".join(f for f, ok in s["focus_gates"].items() if not ok)
                 + ": the answer key has not been asked that question)"),
              f"estimated input tokens: {e['input_tokens']:,} by record size ({e['tokens_per_case']} per case, a sample"
              f" of {e['sampled']}), ${e['cost_usd']:.4f}; {e['measured_input_tokens']:,} at the measured"
              f" {e['measured_tokens_per_case']} (${e['measured_cost_usd']:.4f});"
              f" ${jev.PRICE_PER_MTOK_INPUT} per million input tokens",
              f"estimated time: {e['seconds'] / 60:.1f} min",
              "accept: " + (f"the stored policy {s['policy']['decided_by'] or s['policy']['thresholds']}"
                            f" (from {s['policy']['from']})" if s["policy"] else "NOTHING (no policy was ever applied)")]
    if s.get("request") is not None:
        u = s["example_unit"]
        lines += ["", f"The request for {u['kind']} {u['key']} (anchor message {u['anchor']}), exactly as it would be sent:",
                  f"POST {jev.ENDPOINT}",
                  f"Authorization: Bearer <the Keychain item {jev.KEY_NAME!r}; not read in a dry run>",
                  "Content-Type: application/json", "", json.dumps(s["request"], ensure_ascii=False, indent=2)]
    return "\n".join(lines)
