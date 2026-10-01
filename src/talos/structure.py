"""The mailbox structure planner: where every mail should go in the new Talos/ structure.

It is dry. It computes and proposes; it writes nothing to any mailbox.

    rules/structure.json ──► plan() ──► structure_plan (one row per email message)
                                          │
                                          ├─► report(): counts per target, the inbox before and after,
                                          │   To sort, and how the old labels and folders map
                                          └─► changesets(): Gmail add_label / archive changesets,
                                              planned and never committed (the owner reviews each)

The rules are data (rules/structure.json, versioned): per account, an ordered list of targets. A
target has a path (a Gmail label, later an Exchange folder, under Talos/) and conditions over the
message's effective values; the first target whose conditions all hold places the message. Three
kinds of place:

- inbox: the message stays in the inbox and gets no label. Only mail that is in the inbox now can
  be placed there: the planner never puts mail back into an inbox.
- label: the message gets the target's label (Gmail) or, later, moves to the folder (Exchange).
  When it is in the inbox now, it also leaves the inbox (leaves_inbox).
- leave: never touched (the owner's sent mail, drafts, Exchange's own folders).

The values are the ones that count (effective_message_assignment's ranking: human > rule >
accepted model > import, the message's own before its thread's, the newest first), gathered for
every message in one grouped pass. sender is sender_kind, else the side of the origin (person,
person_via_system and list are people; the rest machine), the way People / Automated reads it.
kind (the coarse type) is used when a message has one, else the condition's `else` (type, then
origin): the rules do not depend on kind being there.

Everything is SQL: the rules compile to one CASE per account over one row of features per
message, so a full plan of the archive is one INSERT … SELECT. preview() runs the same SELECT
without storing it, which is how the numbers are read from the real archive in a read-only
transaction.

Only email is placed. Teams messages are not mail, have no mailbox place, and are never selected.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from talos import changesets as cs
from talos import personal

log = logging.getLogger("talos.structure")

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "rules" / "structure.json"
NAMESPACE = "Talos/"
INBOX = "Inbox"
PLACES = ("inbox", "label", "leave")
TO_SORT = "Talos/To sort"
UNPLACED = "(no rule)"   # the rule id when no target matched (the file lost its catch-all)
PERSON_ORIGINS = ("person", "person_via_system", "list")  # talos.enrich.PERSON_ORIGINS
# One-value dimensions read per message, and the many-value ones (every value counts).
ONE_DIMS = ("sender_kind", "sphere", "form", "keep", "origin", "type", "topic", "value", "kind")
MANY_DIMS = ("ask",)
# Condition fields and the feature column each reads (over the features row f).
SCALAR = {d: f"f.{d}" for d in ONE_DIMS} | {"sender": "f.sender", "direction": "f.direction"}
ARRAY = {"ask": "f.ask", "folder": "f.folders", "label": "f.labels"}
BOOL = {"in_inbox": "f.in_inbox", "flagged": "f.flagged", "replied": "f.replied", "ask_proposed": "f.ask_proposed"}
NUMBER = {"age_days": "f.age_days"}
FIELDS = {**SCALAR, **ARRAY, **BOOL, **NUMBER}
# The mirror's names that are not a place of the owner's: Gmail's All Mail, and the labels Gmail sets itself.
NOT_OLD = ("[all]", "\\Important", "\\Sent", "\\Draft", "\\Starred")


class StructureError(ValueError):
    pass


@dataclass
class Target:
    id: str
    place: str
    path: str
    when: list[dict]
    why: str | None = None


@dataclass
class AccountRules:
    id: str
    name: str
    inbox_labels: list[str]
    inbox_folders: list[str]
    targets: list[Target]
    old: dict[str, str] = field(default_factory=dict)


@dataclass
class Rules:
    version: int
    sha: str
    accounts: dict[str, AccountRules]
    macros: dict[str, list[dict]]
    path: str


# ---------------------------------------------------------------------------- the rules file

def load(path: str | Path | None = None) -> Rules:
    """Read and check the structure rules (the owner's, else rules/structure.json). Refuses anything it could
    not place safely."""
    # the owner's structure (TALOS_HOME/config/structure.json, talos.personal), else the repository's
    p = Path(path) if path else personal.find("structure.json", DEFAULT_PATH)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StructureError(f"cannot read the structure rules {p}: {exc}") from exc
    return parse(raw, path=str(p))


def parse(raw: dict, *, path: str = "<data>") -> Rules:
    if not isinstance(raw, dict) or not isinstance(raw.get("version"), int):
        raise StructureError("the structure rules need an integer version")
    macros = raw.get("conditions") or {}
    blocks = raw.get("blocks") or {}
    accounts: dict[str, AccountRules] = {}
    for acc, a in (raw.get("accounts") or {}).items():
        targets: list[Target] = []
        for item in a.get("targets") or []:
            if "use" in item:
                if item["use"] not in blocks:
                    raise StructureError(f"{acc}: no block {item['use']!r}")
                targets.extend(_target(acc, t) for t in blocks[item["use"]])
            else:
                targets.append(_target(acc, item))
        ids = [t.id for t in targets]
        if len(set(ids)) != len(ids):
            raise StructureError(f"{acc}: target ids repeat: {sorted({i for i in ids if ids.count(i) > 1})}")
        inbox = a.get("inbox") or {}
        rules = AccountRules(acc, a.get("name") or acc, list(inbox.get("labels") or []), list(inbox.get("folders") or []),
                             targets, dict(a.get("old") or {}))
        if not rules.inbox_labels and not rules.inbox_folders:
            raise StructureError(f"{acc}: say where the inbox is (inbox.labels or inbox.folders)")
        accounts[acc] = rules
    if not accounts:
        raise StructureError("the structure rules name no account")
    out = Rules(raw["version"], _sha(raw), accounts, macros, path)
    for a in accounts.values():  # compile once, so a bad condition is refused on load
        for t in a.targets:
            _Compiler(out).all(t.when)
    return out


def _target(acc: str, t: dict) -> Target:
    for k in ("id", "place", "path"):
        if not t.get(k):
            raise StructureError(f"{acc}: every target needs id, place and path ({t!r})")
    if t["place"] not in PLACES:
        raise StructureError(f"{acc}/{t['id']}: place is one of {', '.join(PLACES)}")
    if t["place"] == "inbox" and t["path"] != INBOX:
        raise StructureError(f"{acc}/{t['id']}: an inbox target's path is {INBOX!r}")
    if t["place"] == "label" and not (t["path"].startswith(NAMESPACE) and len(t["path"]) > len(NAMESPACE)):
        raise StructureError(f"{acc}/{t['id']}: labels and folders are written only under {NAMESPACE}; {t['path']!r} is not")
    if not isinstance(t.get("when", []), list):
        raise StructureError(f"{acc}/{t['id']}: when is a list of conditions")
    return Target(t["id"], t["place"], t["path"], list(t.get("when") or []), t.get("why"))


def _sha(raw: dict) -> str:
    return hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------- conditions → SQL

class _Compiler:
    """Conditions to SQL over the features row f, with named parameters (p0, p1, …)."""

    def __init__(self, rules: Rules, params: dict | None = None):
        self.rules = rules
        self.params = params if params is not None else {}

    def param(self, value) -> str:
        name = f"p{len(self.params)}"
        self.params[name] = value
        return f"%({name})s"

    def all(self, conds: list[dict]) -> str:
        if not conds:
            return "true"
        return "(" + " and ".join(self.cond(c) for c in conds) + ")"

    def cond(self, c: dict) -> str:
        if not isinstance(c, dict):
            raise StructureError(f"a condition is an object: {c!r}")
        if "use" in c:
            if c["use"] not in self.rules.macros:
                raise StructureError(f"no named condition {c['use']!r}")
            return self.all(self.rules.macros[c["use"]])
        if "any" in c:
            return "(" + " or ".join(self.cond(x) for x in c["any"]) + ")" if c["any"] else "false"
        if "all" in c:
            return self.all(c["all"])
        f = c.get("field")
        if f not in FIELDS:
            raise StructureError(f"unknown field {f!r} (fields: {', '.join(sorted(FIELDS))})")
        expr = FIELDS[f]
        ops = [k for k in ("in", "not_in", "prefix", "set", "is", "lt", "gt") if k in c]
        if len(ops) != 1:
            raise StructureError(f"{f}: one operator per condition (in, not_in, prefix, set, is, lt, gt): {c!r}")
        op, v = ops[0], c[ops[0]]
        if f in SCALAR:
            test = self._scalar(f, expr, op, v)
            if "else" in c:
                return f"(case when {expr} is not null then {test} else {self.cond(c['else'])} end)"
            return test
        if "else" in c:
            raise StructureError(f"{f}: else applies to one-value fields only")
        if f in ARRAY:
            return self._array(f, expr, op, v)
        if f in BOOL:
            if op != "is" or not isinstance(v, bool):
                raise StructureError(f"{f}: use {{\"is\": true}} or {{\"is\": false}}")
            return f"({expr} is {'true' if v else 'not true'})"
        if op not in ("lt", "gt") or not isinstance(v, (int, float)):
            raise StructureError(f"{f}: use lt or gt with a number")
        return f"coalesce({expr} {'<' if op == 'lt' else '>'} {self.param(v)}, false)"

    def _list(self, f: str, op: str, v) -> str:
        if not isinstance(v, list) or not v or not all(isinstance(x, str) for x in v):
            raise StructureError(f"{f}: {op} takes a list of text values")
        return self.param(v) + "::text[]"

    def _scalar(self, f: str, expr: str, op: str, v) -> str:
        if op == "set":
            if not isinstance(v, bool):
                raise StructureError(f"{f}: set is true or false")
            return f"({expr} is {'not ' if v else ''}null)"
        if op in ("is", "lt", "gt"):
            raise StructureError(f"{f}: use in, not_in, prefix or set")
        p = self._list(f, op, v)
        if op == "in":
            return f"coalesce({expr} = any({p}), false)"
        if op == "not_in":
            return f"not coalesce({expr} = any({p}), false)"
        return f"coalesce(exists (select 1 from unnest({p}) x where starts_with({expr}, x)), false)"

    def _array(self, f: str, expr: str, op: str, v) -> str:
        if op == "set":
            if not isinstance(v, bool):
                raise StructureError(f"{f}: set is true or false")
            return f"(cardinality({expr}) {'>' if v else '='} 0)"
        if op in ("is", "lt", "gt"):
            raise StructureError(f"{f}: use in, not_in, prefix or set")
        p = self._list(f, op, v)
        if op == "in":
            return f"({expr} && {p})"
        if op == "not_in":
            return f"not ({expr} && {p})"
        return f"exists (select 1 from unnest({expr}) y, unnest({p}) x where starts_with(y, x))"


BOOL_TEXT = {"in_inbox": ("in the inbox now", "not in the inbox"), "flagged": ("flagged or starred by you", "not flagged"),
             "replied": ("you replied in the thread since", "you have not replied since"),
             "ask_proposed": ("Jev proposed an ask", "no proposed ask")}


def describe(c: dict) -> str:
    """A condition in words, for the Structure page and the report."""
    if "use" in c:
        return f"[{c['use'].replace('_', ' ')}]"
    if "any" in c:
        return "(" + " or ".join(describe(x) for x in c["any"]) + ")"
    if "all" in c:
        return "(" + " and ".join(describe(x) for x in c["all"]) + ")"
    f = c.get("field", "?").replace("_", " ")
    if "in" in c:
        vals = c["in"]
        text = f"{f} is {vals[0]}" if len(vals) == 1 else f"{f} is one of {', '.join(vals)}"
    elif "not_in" in c:
        text = f"{f} is not {', '.join(c['not_in'])}"
    elif "prefix" in c:
        text = f"{f} starts with {' or '.join(c['prefix'])}"
    elif "set" in c:
        text = f"{f} is {'set' if c['set'] else 'not decided'}"
    elif "is" in c:
        words = BOOL_TEXT.get(c["field"], (f, f"not {f}"))
        text = words[0] if c["is"] else words[1]
    elif "lt" in c:
        text = f"under {c['lt']} days old" if c["field"] == "age_days" else f"{f} under {c['lt']}"
    elif "gt" in c:
        text = f"over {c['gt']} days old" if c["field"] == "age_days" else f"{f} over {c['gt']}"
    else:
        text = f
    if "else" in c:
        text += f" [when it has no {f}: {describe(c['else'])}]"
    return text


def expand(rules: Rules, conds: list[dict]) -> list[dict]:
    """The conditions with named ones written out, for showing."""
    out = []
    for c in conds:
        out.extend(expand(rules, rules.macros[c["use"]]) if "use" in c else [c])
    return out


# ---------------------------------------------------------------------------- features and placement

def _features_sql(rules: Rules, accounts: list[str], params: dict, *, ids: list[int] | None = None,
                  now: datetime | None = None) -> str:
    """One row per email message of the accounts: its effective values, where it is on the server,
    whether the owner replied, how old it is. The CTE the placement reads (as f)."""
    params["accounts"] = accounts
    params["one_dims"] = list(ONE_DIMS)
    params["all_dims"] = list(ONE_DIMS + MANY_DIMS)
    params["person_origins"] = list(PERSON_ORIGINS)
    params["now"] = now or datetime.now(timezone.utc)
    inbox = []
    for i, acc in enumerate(accounts):
        a = rules.accounts[acc]
        params[f"ib_acc{i}"], params[f"ib_lab{i}"], params[f"ib_fol{i}"] = acc, a.inbox_labels, a.inbox_folders
        inbox.append(f"when %(ib_acc{i})s then (l.labels && %(ib_lab{i})s::text[]"
                     f" or coalesce(l.folder_path, l.folder) = any(%(ib_fol{i})s::text[]))")
    only = ""
    if ids is not None:
        params["ids"] = list(ids)
        only = " and m.id = any(%(ids)s)"
    one = ", ".join(f"max(c.value) filter (where c.dim = '{d}' and c.rn = 1) as {d}" for d in ONE_DIMS)
    many = ", ".join(f"array_agg(distinct c.value) filter (where c.dim = '{d}') as {d}" for d in MANY_DIMS)
    return f"""
    base as materialized (
        select m.id, m.account_id, m.thread_id, m.received_at, m.direction from message m
        where m.medium = 'email' and m.account_id = any(%(accounts)s){only}),
    c as (
        select b.id as mid, a.dimension_id as dim, a.value,
               row_number() over (partition by b.id, a.dimension_id
                                  order by case a.source_kind when 'human' then 4 when 'rule' then 3
                                                              when 'model' then 2 else 1 end desc,
                                           (a.entity_id = b.id) desc, a.created_at desc, a.id desc) as rn
        from base b join assignment a on a.entity_id = any(array[b.id, b.thread_id]) and a.status = 'active'
                                     and a.dimension_id = any(%(all_dims)s)),
    v as (select c.mid, {one}, {many} from c group by c.mid),
    loc as (
        select l.message_id,
               bool_or(case l.account_id {' '.join(inbox)} else false end) as in_inbox,
               bool_or('flagged' = any(l.flags)) as flagged, bool_or('answered' = any(l.flags)) as answered,
               array_agg(distinct coalesce(l.folder_path, l.folder)) as folders
        from message_location l join base b on b.id = l.message_id where l.present group by l.message_id),
    lab as (
        select l.message_id, array_agg(distinct x) as labels
        from message_location l join base b on b.id = l.message_id cross join unnest(l.labels) x
        where l.present group by l.message_id),
    f as materialized (
        select b.id, b.account_id, b.received_at, b.direction,
               coalesce(loc.in_inbox, false) as in_inbox, coalesce(loc.flagged, false) as flagged,
               coalesce(loc.folders, '{{}}') as folders, coalesce(lab.labels, '{{}}') as labels,
               {', '.join('v.' + d for d in ONE_DIMS)},
               coalesce(v.sender_kind, case when v.origin = any(%(person_origins)s) then 'people'
                                            when v.origin is not null then 'machine' end) as sender,
               coalesce(v.ask, '{{}}') as ask,
               exists (select 1 from assignment a where a.entity_id = any(array[b.id, b.thread_id])
                       and a.dimension_id = 'ask' and a.source_kind = 'model' and a.status = 'proposed') as ask_proposed,
               (coalesce(loc.answered, false) or exists (
                   select 1 from message r where r.thread_id = b.thread_id and r.direction = 'out'
                   and r.received_at > b.received_at)) as replied,
               extract(epoch from (%(now)s::timestamptz - b.received_at)) / 86400.0 as age_days
        from base b left join v on v.mid = b.id left join loc on loc.message_id = b.id
        left join lab on lab.message_id = b.id)"""


def _placement_sql(rules: Rules, accounts: list[str], params: dict, *, ids: list[int] | None = None,
                   now: datetime | None = None, extra: str = "") -> str:
    """SELECT message_id, account_id, rule_id, place, target, in_inbox, leaves_inbox (and extra columns
    over f) for every email message of the accounts: the first matching target, per account."""
    comp = _Compiler(rules, params)
    features = _features_sql(rules, accounts, params, ids=ids, now=now)
    per_account, t_acc, t_id, t_place, t_path = [], [], [], [], []
    for i, acc in enumerate(accounts):
        whens = []
        for t in rules.accounts[acc].targets:
            cond = comp.all(t.when)
            if t.place == "inbox":  # the inbox is never refilled: only mail in it now stays
                cond = f"(f.in_inbox and {cond})"
            whens.append(f"when {cond} then {comp.param(t.id)}")
            t_acc.append(acc), t_id.append(t.id), t_place.append(t.place), t_path.append(t.path)
        params[f"acc{i}"] = acc
        per_account.append(f"when %(acc{i})s then (case {' '.join(whens)} end)" if whens else f"when %(acc{i})s then null")
    params.update(t_acc=t_acc, t_id=t_id, t_place=t_place, t_path=t_path, unplaced=UNPLACED, to_sort=TO_SORT)
    return f"""
    with {features},
    r as (select f.id, f.account_id, f.in_inbox, (case f.account_id {' '.join(per_account)} end) as rule_id{extra}
          from f),
    t as (select * from unnest(%(t_acc)s::text[], %(t_id)s::text[], %(t_place)s::text[], %(t_path)s::text[])
                       as t(account_id, rule_id, place, path))
    select r.id as message_id, r.account_id, coalesce(r.rule_id, %(unplaced)s) as rule_id,
           coalesce(t.place, 'label') as place, coalesce(t.path, %(to_sort)s) as target, r.in_inbox,
           (r.in_inbox and coalesce(t.place, 'label') = 'label') as leaves_inbox{', r.' + ', r.'.join(_extra_names(extra)) if extra else ''}
    from r left join t on t.account_id = r.account_id and t.rule_id = r.rule_id"""


def _extra_names(extra: str) -> list[str]:
    return [part.strip().split()[-1].split(".")[-1] for part in extra.strip(", ").split(", ")] if extra else []


def _accounts(conn: psycopg.Connection, rules: Rules, account: str | None) -> list[str]:
    known = {r["id"]: r["provider"] for r in conn.execute("select id, provider from account")}
    if account:
        if account not in rules.accounts:
            if known.get(account) == "teams":
                raise StructureError(f"{account} is Teams: Teams messages are not mail and are never placed")
            raise StructureError(f"the structure rules have no account {account!r} (they have: {', '.join(rules.accounts)})")
        if account not in known:
            raise StructureError(f"no account {account!r} in this database")
        return [account]
    return [a for a in rules.accounts if a in known and known[a] != "teams"]


# ---------------------------------------------------------------------------- plan, preview, refresh

def plan(conn: psycopg.Connection, rules: Rules | None = None, *, account: str | None = None,
         now: datetime | None = None) -> dict:
    """Compute the place of every email message of the account (or all planned accounts) and store it
    in structure_plan, replacing the account's earlier plan. Returns the report, with timings."""
    rules = rules or load()
    out = {}
    for acc in _accounts(conn, rules, account):
        out[acc] = _store(conn, rules, acc, mode="full", now=now)
    return out


def refresh(conn: psycopg.Connection, rules: Rules | None = None, *, account: str,
            now: datetime | None = None) -> dict:
    """After a sync: place what is new, and look again at what may have moved on.

    New messages (no plan row), messages placed in the inbox (they age: 14 days later they are
    filed), and recent To sort messages (their values often arrive a day after the mail). When the
    rules file changed since the last full plan, the whole account is planned again."""
    rules = rules or load()
    last = conn.execute("select rules_sha from structure_run where account_id = %s and mode = 'full'"
                        " order by id desc limit 1", (account,)).fetchone()
    if not last or last["rules_sha"] != rules.sha:
        return _store(conn, rules, account, mode="full", now=now)
    ids = [r["id"] for r in conn.execute(
        "select m.id from message m where m.account_id = %(a)s and m.medium = 'email'"
        " and not exists (select 1 from structure_plan p where p.message_id = m.id)"
        " union select p.message_id from structure_plan p join message m on m.id = p.message_id"
        " where p.account_id = %(a)s and (p.place = 'inbox'"
        "   or (p.target = %(ts)s and m.received_at > %(now)s::timestamptz - interval '30 days'))",
        {"a": account, "ts": TO_SORT, "now": now or datetime.now(timezone.utc)})]
    return _store(conn, rules, account, mode="incremental", ids=ids, now=now)


def _store(conn: psycopg.Connection, rules: Rules, account: str, *, mode: str, ids: list[int] | None = None,
           now: datetime | None = None) -> dict:
    t0 = time.monotonic()
    params: dict = {"ver": rules.version, "sha": rules.sha}
    select = _placement_sql(rules, [account], params, ids=ids, now=now)
    with conn.transaction():
        conn.execute("set local work_mem = '256MB'")
        placed = conn.execute(
            f"insert into structure_plan (message_id, account_id, rule_id, place, target, in_inbox, leaves_inbox,"
            f" rules_version, rules_sha) select p.message_id, p.account_id, p.rule_id, p.place, p.target, p.in_inbox,"
            f" p.leaves_inbox, %(ver)s, %(sha)s from ({select}) p"
            " on conflict (message_id) do update set rule_id = excluded.rule_id, place = excluded.place,"
            " target = excluded.target, in_inbox = excluded.in_inbox, leaves_inbox = excluded.leaves_inbox,"
            " rules_version = excluded.rules_version, rules_sha = excluded.rules_sha, computed_at = now(),"
            " changed_at = case when structure_plan.target is distinct from excluded.target"
            "                    or structure_plan.leaves_inbox is distinct from excluded.leaves_inbox"
            "                   then now() else structure_plan.changed_at end", params).rowcount
        if mode == "full":  # messages no longer placed (gone, or no longer email) leave the plan
            conn.execute("delete from structure_plan where account_id = %s and computed_at < now()", (account,))
        seconds = round(time.monotonic() - t0, 2)
        summary = _report_rows(conn, rules, account, "plan") if mode == "full" else {}
        conn.execute("insert into structure_run (account_id, mode, rules_version, rules_sha, seconds, placed, summary)"
                     " values (%s, %s, %s, %s, %s, %s, %s)",
                     (account, mode, rules.version, rules.sha, seconds, placed, Jsonb(summary)))
        # One incremental run per sync (every five minutes): a week of them is plenty.
        conn.execute("delete from structure_run where account_id = %s and mode = 'incremental'"
                     " and started_at < now() - interval '7 days'", (account,))
    total = round(time.monotonic() - t0, 2)
    log.info("structure plan account=%s mode=%s placed=%d seconds=%.2f", account, mode, placed, total)
    return {"account": account, "mode": mode, "placed": placed, "seconds": seconds, "with_report_seconds": total,
            **({"report": summary} if summary else {})}


def preview(conn: psycopg.Connection, rules: Rules | None = None, *, account: str | None = None,
            now: datetime | None = None) -> dict:
    """The same plan and report, computed and not stored: plain SELECTs, so it runs in a read-only
    transaction against the real archive."""
    rules = rules or load()
    out = {}
    for acc in _accounts(conn, rules, account):
        t0 = time.monotonic()
        out[acc] = _report_rows(conn, rules, acc, "preview", now=now)
        out[acc]["seconds"] = round(time.monotonic() - t0, 2)
    return out


# ---------------------------------------------------------------------------- the report

def _report_rows(conn: psycopg.Connection, rules: Rules, account: str, source: str, *,
                 now: datetime | None = None) -> dict:
    """Counts per target, the inbox before and after, To sort, and the old labels and folders."""
    a = rules.accounts[account]
    params: dict = {"not_old": list(NOT_OLD), "om_old": list(a.old), "om_new": list(a.old.values())}
    if source == "plan":
        params["acc"] = account
        p_sql = ("select sp.account_id, sp.place, sp.target, sp.rule_id, sp.in_inbox, sp.leaves_inbox,"
                 " coalesce((select array_agg(distinct x) from message_location l,"
                 " unnest(array[coalesce(l.folder_path, l.folder)] || l.labels) x"
                 " where l.message_id = sp.message_id and l.present), '{}') as olds"
                 " from structure_plan sp where sp.account_id = %(acc)s")
        head = f"with p as materialized ({p_sql})"
    else:
        inner = _placement_sql(rules, [account], params, now=now, extra=", f.folders || f.labels as olds")
        head = f"with p as materialized ({inner})"
    rows = conn.execute(
        f"""{head},
        q as (select p.*, array(select o from unnest(p.olds) o where not o = any(%(not_old)s)) as old,
                     exists (select 1 from unnest(p.olds) o
                             join unnest(%(om_old)s::text[], %(om_new)s::text[]) as om(old, new) on om.old = o
                             where om.new = p.target) as same from p)
        select 't' as k, place, target, rule_id, in_inbox, leaves_inbox, null as old, same, count(*)::int as n
        from q group by place, target, rule_id, in_inbox, leaves_inbox, same
        union all
        select 'o', null, target, null, null, null, o, null, count(*)::int from q, unnest(q.old) o
        group by target, o""", params).fetchall()
    return _summarise(rules, account, rows)


def _summarise(rules: Rules, account: str, rows: list[dict]) -> dict:
    """The report from the counting query's two kinds of row, told apart by k: 't' rows count the
    messages per target (with the rule that placed them, whether they sit in the inbox now, whether
    they would leave it, and whether their old place already maps to this target: same); 'o' rows
    count, per old place (a label or folder of before Talos/), where its messages go now. Totals, the
    inbox before and after, and each old place's main destination come out of them."""
    a = rules.accounts[account]
    targets: dict[str, dict] = {}
    order = {t.path: i for i, t in reversed(list(enumerate(a.targets)))}
    total = inbox_before = inbox_after = same = 0
    for r in rows:
        if r["k"] != "t":
            continue
        n = r["n"]
        total += n
        t = targets.setdefault(r["target"], {"target": r["target"], "place": r["place"], "count": 0, "in_inbox": 0,
                                             "leaves_inbox": 0, "rules": Counter()})
        t["count"] += n
        t["rules"][r["rule_id"]] += n
        if r["in_inbox"]:
            inbox_before += n
            t["in_inbox"] += n
            if r["leaves_inbox"]:
                t["leaves_inbox"] += n
            else:
                inbox_after += n
        if r["same"]:
            same += n
    old: dict[str, dict] = {}
    for r in rows:
        if r["k"] != "o":
            continue
        o = old.setdefault(r["old"], {"old": r["old"], "count": 0, "maps_to": a.old.get(r["old"]), "same": 0, "targets": Counter()})
        o["count"] += r["n"]
        o["targets"][r["target"]] += r["n"]
        if o["maps_to"] == r["target"]:
            o["same"] += r["n"]
    by_target = sorted(targets.values(), key=lambda t: (order.get(t["target"], 999), t["target"]))
    for t in by_target:
        t["rules"] = dict(t["rules"].most_common())
    olds = sorted(old.values(), key=lambda o: -o["count"])
    for o in olds:
        o["targets"] = [{"target": k, "count": v} for k, v in o["targets"].most_common(4)]
    return {"account": account, "name": a.name, "rules_version": rules.version, "rules_sha": rules.sha,
            "total": total, "inbox_before": inbox_before, "inbox_after": inbox_after,
            "leaves_inbox": inbox_before - inbox_after,
            "to_sort": sum(t["count"] for t in by_target if t["target"] == TO_SORT),
            "left_alone": sum(t["count"] for t in by_target if t["place"] == "leave"),
            "same_place": same, "targets": by_target, "old": olds}


def report(conn: psycopg.Connection, account: str | None = None) -> dict:
    """The stored plan's report: the last full run's summary, with the live counts per target."""
    out = {}
    accs = [account] if account else [r["account_id"] for r in conn.execute(
        "select distinct account_id from structure_plan order by 1")]
    for acc in accs:
        run = conn.execute("select * from structure_run where account_id = %s and mode = 'full' order by id desc limit 1",
                           (acc,)).fetchone()
        live = conn.execute(
            "select place, target, count(*)::int as n, count(*) filter (where in_inbox)::int as in_inbox,"
            " count(*) filter (where leaves_inbox)::int as leaves_inbox, max(computed_at) as computed_at"
            " from structure_plan where account_id = %s group by place, target", (acc,)).fetchall()
        last = conn.execute("select started_at, mode, seconds, placed from structure_run where account_id = %s"
                            " order by id desc limit 1", (acc,)).fetchone()
        out[acc] = {"summary": run["summary"] if run else None, "full_run": _run(run), "last_run": _run(last),
                    "live": live}
    return out


def _run(r: dict | None) -> dict | None:
    return None if not r else {k: r[k] for k in ("started_at", "mode", "seconds", "placed") if k in r} | (
        {"rules_version": r["rules_version"], "rules_sha": r["rules_sha"]} if "rules_version" in r else {})


def format_report(rep: dict) -> str:
    """A report (from plan() or preview()) as plain text."""
    lines = []
    for acc, r in rep.items():
        r = r.get("report", r)
        if "targets" not in r:
            lines.append(f"{acc}: placed {r.get('placed')} in {r.get('seconds')} s ({r.get('mode')})")
            continue
        lines.append(f"\n{r['name']} ({acc}): {r['total']:,} email messages · rules v{r['rules_version']}"
                     f" ({r['rules_sha']})" + (f" · {r['seconds']} s" if "seconds" in r else ""))
        lines.append(f"  inbox before {r['inbox_before']:,} → after {r['inbox_after']:,}"
                     f" ({r['leaves_inbox']:,} leave it) · To sort {r['to_sort']:,} · left alone {r['left_alone']:,}"
                     f" · already in an old place that maps here {r['same_place']:,}")
        for t in r["targets"]:
            rules_txt = ", ".join(f"{k} {v:,}" for k, v in t["rules"].items()) if len(t["rules"]) > 1 else ""
            lines.append(f"  {t['count']:>8,}  {t['target']:<42} in inbox now {t['in_inbox']:>7,}"
                         + (f"  {rules_txt}" if rules_txt else ""))
        lines.append("  old label/folder → new place (top targets)")
        for o in r["old"]:
            tops = "; ".join(f"{x['target'].removeprefix('Talos/')} {x['count']:,}" for x in o["targets"][:3])
            mapped = f" [mapped to {o['maps_to'].removeprefix('Talos/')}: {o['same']:,} agree]" if o["maps_to"] else ""
            lines.append(f"  {o['count']:>8,}  {o['old']:<28} {tops}{mapped}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------- why is this message here

def why(conn: psycopg.Connection, message_id: int, rules: Rules | None = None, *, now: datetime | None = None) -> dict:
    """The features of one message, every target of its account tested in order (each condition
    with its result), the first match, and the stored plan row to compare with."""
    rules = rules or load()
    m = conn.execute("select id, account_id, medium from message where id = %s", (message_id,)).fetchone()
    if not m:
        raise StructureError(f"no message {message_id}")
    if m["medium"] != "email":
        return {"message_id": message_id, "account": m["account_id"], "placed": False,
                "reason": "a Teams message: not mail, never placed"}
    if m["account_id"] not in rules.accounts:
        return {"message_id": message_id, "account": m["account_id"], "placed": False,
                "reason": f"the structure rules have no account {m['account_id']!r}"}
    a = rules.accounts[m["account_id"]]
    params: dict = {}
    features = _features_sql(rules, [a.id], params, ids=[message_id], now=now)
    comp = _Compiler(rules, params)
    cols, layout = [], []
    for i, t in enumerate(a.targets):
        conds = expand(rules, t.when)
        whole = comp.all(t.when)
        cols.append(f"({'f.in_inbox and ' if t.place == 'inbox' else ''}{whole}) as t{i}")
        parts = []
        if t.place == "inbox":
            parts.append(("in the inbox now", "f.in_inbox"))
        parts += [(describe(c), comp.cond(c)) for c in conds]
        for j, (_, sql) in enumerate(parts):
            cols.append(f"coalesce({sql}, false) as t{i}_{j}")
        layout.append((t, [text for text, _ in parts]))
    row = conn.execute(f"with {features} select f.*, {', '.join(cols)} from f", params).fetchone()
    tests, first = [], None
    for i, (t, texts) in enumerate(layout):
        ok = bool(row[f"t{i}"])
        if ok and first is None:
            first = t
        tests.append({"id": t.id, "path": t.path, "place": t.place, "why": t.why, "matched": ok,
                      "first": ok and first is t,
                      "conditions": [{"text": text, "ok": bool(row[f"t{i}_{j}"])} for j, text in enumerate(texts)]})
    feats = {k: row[k] for k in ("in_inbox", "flagged", "folders", "labels", "sender", *ONE_DIMS, "ask",
                                 "ask_proposed", "replied", "direction")}
    feats["age_days"] = round(float(row["age_days"]), 1) if row["age_days"] is not None else None
    values = conn.execute(
        "select e.dimension_id, e.value, e.source_kind, e.source_ref, e.via from effective_message_assignment e"
        " where e.message_id = %s and e.dimension_id = any(%s) order by e.dimension_id",
        (message_id, list(ONE_DIMS + MANY_DIMS))).fetchall()
    stored = conn.execute("select place, target, rule_id, in_inbox, leaves_inbox, rules_version, rules_sha,"
                          " computed_at, changed_at from structure_plan where message_id = %s", (message_id,)).fetchone()
    now_target = {"place": first.place, "target": first.path, "rule_id": first.id} if first else \
        {"place": "label", "target": TO_SORT, "rule_id": UNPLACED}
    return {"message_id": message_id, "account": a.id, "placed": True, "features": feats, "values": values,
            "tests": tests, "now": now_target, "stored": stored,
            "stale": bool(stored and (stored["target"] != now_target["target"] or stored["rules_sha"] != rules.sha))}


# ---------------------------------------------------------------------------- the tree and examples

def tree(rules: Rules, account: str, counts: dict[str, dict]) -> list[dict]:
    """The target structure as nested nodes, each with the messages under it. counts: path ->
    {count, in_inbox, leaves_inbox}. Every target path is a node, with or without messages."""
    a = rules.accounts[account]
    root: dict = {"children": {}}
    rules_of: dict[str, list[dict]] = defaultdict(list)
    for t in a.targets:
        rules_of[t.path].append({"id": t.id, "place": t.place, "why": t.why,
                                 "conditions": [describe(c) for c in expand(rules, t.when)]})
    paths = list(dict.fromkeys([t.path for t in a.targets] + list(counts)))
    for path in paths:
        node = root
        parts = path.split("/") if path.startswith(NAMESPACE) else [path]
        for depth, part in enumerate(parts):
            full = "/".join(parts[:depth + 1])
            node = node["children"].setdefault(part, {"name": part, "path": full, "children": {}, "count": 0,
                                                      "in_inbox": 0, "leaves_inbox": 0, "rules": [], "leaf": False})
        node["leaf"] = True
        node["rules"] = rules_of.get(path, [])
        node["place"] = (rules_of.get(path) or [{"place": "label"}])[0]["place"]

    def total(node: dict) -> None:
        kids = list(node["children"].values())
        for k in kids:
            total(k)
        own = counts.get(node.get("path"), {}) if node.get("leaf") else {}
        for key in ("count", "in_inbox", "leaves_inbox"):
            node[key] = own.get(key, 0) + sum(k[key] for k in kids)
        node["children"] = kids

    total(root)
    # The inbox first, then the Talos/ tree, then what is left alone.
    rank = {"inbox": 0, "label": 1, "leave": 2}
    return sorted(root["children"], key=lambda n: rank.get(n.get("place") if n.get("leaf") else "label", 1))


def examples(conn: psycopg.Connection, account: str, target: str, *, rule: str | None = None,
             limit: int = 25, offset: int = 0) -> dict:
    """Messages placed at a target (or under it, for a folder node), newest first."""
    params = {"a": account, "t": target, "under": target + "/%", "r": rule, "limit": limit, "offset": offset}
    where = "p.account_id = %(a)s and (p.target = %(t)s or p.target like %(under)s)" + (
        " and p.rule_id = %(r)s" if rule else "")
    total = conn.execute(f"select count(*)::int as n from structure_plan p where {where}", params).fetchone()["n"]
    rows = conn.execute(
        f"select m.id, m.account_id, m.medium, m.received_at, m.from_name, m.from_address, m.subject, m.snippet,"
        f" p.target, p.rule_id, p.in_inbox, p.leaves_inbox from structure_plan p join message m on m.id = p.message_id"
        f" where {where} order by m.received_at desc nulls last limit %(limit)s offset %(offset)s", params).fetchall()
    return {"total": total, "rows": rows}


# ---------------------------------------------------------------------------- changesets

def changesets(conn: psycopg.Connection, account: str, *, target: str | None = None, limit: int | None = None,
               new_since: datetime | None = None, archive_unlabelled: bool = False,
               rules: Rules | None = None) -> list[dict]:
    """Planned changesets from the stored plan, for the owner's review. Never committed, never applied.

    Gmail: one add_label changeset per target label (only messages that do not carry it yet), then
    one archive changeset for the messages that should leave the inbox. By default the archive
    takes only messages whose Talos label is already on the server (in the mirror), so nothing
    leaves the inbox before it can be found in its new place: apply the label changesets, sync,
    and run this again for the archive. Messages already in an open changeset for the same
    operation are left out, so running this twice does not double them.

    Exchange is planned only: the work account's write-back is switched on for one agreed use at a time
    (docs/writeback-test-plan.md), and folders under Talos/ are not one of them yet."""
    provider = conn.execute("select provider from account where id = %s", (account,)).fetchone()
    if not provider:
        raise StructureError(f"no account {account!r}")
    if provider["provider"] != "gmail":
        raise StructureError(f"{account} is planned only: no changesets yet. Folders under Talos/ and the moves"
                             " into them wait for your go (docs/mailbox-structure-plan.md)")
    if limit is not None and limit < 1:
        raise StructureError("--limit is at least 1")
    run = conn.execute("select max(computed_at) as at, max(rules_version) as v from structure_plan where account_id = %s",
                       (account,)).fetchone()
    if not run["at"]:
        raise StructureError(f"no plan for {account} yet: run talos structure plan --account {account}")
    rules = rules or load()
    paths = [t.path for t in rules.accounts[account].targets if t.place == "label"] if account in rules.accounts else []
    targets = [r["target"] for r in conn.execute(
        "select distinct target from structure_plan where account_id = %s and place = 'label'", (account,))]
    targets = sorted(targets, key=lambda p: (paths.index(p) if p in paths else 999, p))
    if target:
        if target not in targets:
            raise StructureError(f"no messages are planned for {target!r} in {account}")
        targets = [target]
    since = {"since": new_since}
    since_sql = " and (m.ingested_at >= %(since)s or p.changed_at >= %(since)s)" if new_since else ""
    made = []
    for path in targets:
        if not path.startswith(NAMESPACE):  # the executor refuses anything else; so does this
            raise StructureError(f"refusing to label outside {NAMESPACE}: {path!r}")
        ids = [r["message_id"] for r in conn.execute(
            f"select p.message_id from structure_plan p join message m on m.id = p.message_id"
            f" where p.account_id = %(a)s and p.place = 'label' and p.target = %(t)s{since_sql}"
            f" and not exists (select 1 from message_location l where l.message_id = p.message_id and l.present"
            f"                 and %(t)s = any(l.labels))"
            f" and not {_OPEN_OP}"
            f" order by m.received_at desc nulls last, p.message_id desc limit %(limit)s",
            {"a": account, "t": path, "op": "add_label", "args": Jsonb({"label": path}), "limit": limit, **since})]
        if ids:
            made.append(_make(conn, account, "add_label", {"label": path}, ids, path, run))
    archive_filter = "" if archive_unlabelled else (
        " and exists (select 1 from message_location l where l.message_id = p.message_id and l.present"
        "             and p.target = any(l.labels))")
    target_sql = " and p.target = %(t)s" if target else ""
    ids = [r["message_id"] for r in conn.execute(
        f"select p.message_id from structure_plan p join message m on m.id = p.message_id"
        f" where p.account_id = %(a)s and p.leaves_inbox and p.place = 'label'{target_sql}{since_sql}"
        f" and exists (select 1 from message_location l where l.message_id = p.message_id and l.present"
        f"             and '\\Inbox' = any(l.labels)){archive_filter}"
        f" and not {_OPEN_OP}"
        f" order by m.received_at desc nulls last, p.message_id desc limit %(limit)s",
        {"a": account, "t": target, "op": "archive", "args": Jsonb({}), "limit": limit, **since})]
    waiting = 0
    if not archive_unlabelled:
        waiting = conn.execute(
            f"select count(*)::int as n from structure_plan p join message m on m.id = p.message_id"
            f" where p.account_id = %(a)s and p.leaves_inbox{target_sql}{since_sql}"
            f" and not exists (select 1 from message_location l where l.message_id = p.message_id and l.present"
            f"                 and p.target = any(l.labels))",
            {"a": account, "t": target, **since}).fetchone()["n"]
    if ids:
        made.append(_make(conn, account, "archive", {}, ids, target, run))
    return made + ([{"note": f"{waiting:,} messages will leave the inbox once their Talos label is on the server:"
                             " apply the label changesets, sync, and run this again"}] if waiting else [])


# A message already in an open changeset (planned, committed or applying) for the same operation.
_OPEN_OP = ("exists (select 1 from changeset_op o join changeset c on c.id = o.changeset_id"
            " where o.message_id = p.message_id and o.op = %(op)s and o.args = %(args)s and o.status = 'pending'"
            " and c.status in ('planned', 'committed', 'applying'))")


def _make(conn: psycopg.Connection, account: str, op: str, args: dict, ids: list[int], target: str | None,
          run: dict) -> dict:
    with conn.transaction():
        cid = cs.create(conn, f"Structure: {op} (planning)", op, args=args, message_ids=ids)
        conn.execute("update changeset set selection = selection || %s, note = %s where id = %s",
                     (Jsonb({"structure": {"account": account, "target": target, "op": op,
                                           "plan_computed_at": run["at"].isoformat(), "rules_version": run["v"]}}),
                      "Made by talos structure changesets from the structure plan. Review it, dry-run it"
                      " (talos changeset dry-run ID), then commit with --max in steps (1, 10, 100, all) and apply."
                      + (" Apply after the label changesets: an archived message should already carry its"
                         " Talos label." if op == "archive" else ""), cid))
        summary = cs.plan(conn, cid)
        n = summary["will_change"]
        noun = "message" if n == 1 else "messages"
        title = (f"Structure: add {args['label']}, {n:,} {noun}" if op == "add_label" else
                 f"Structure: archive {n:,} {noun} out of the inbox" + (f" ({target})" if target else ""))
        conn.execute("update changeset set title = %s where id = %s", (title, cid))
    return {"id": cid, "title": title, "op": op, "args": args, "selected": len(ids), "will_change": n,
            "skipped_because": summary["skipped_because"], "status": "planned"}


def prepared(conn: psycopg.Connection, account: str | None = None) -> list[dict]:
    """The changesets made from the structure plan, newest first. Without the dry run's detail, which the page
    does not show and which runs to megabytes for a large changeset (the Changesets page loads it one at a time)."""
    return conn.execute(
        "select id, title, status, summary - 'dry_run' as summary, created_at, planned_at, committed_at, finished_at,"
        " selection->'structure' as structure from changeset where selection ? 'structure'"
        + (" and selection->'structure'->>'account' = %s" if account else "") + " order by id desc limit 100",
        (account,) if account else ()).fetchall()


# ---------------------------------------------------------------------------- how to make it real

# The steps from a plan to a mailbox that looks like it, per provider. Talos can see the state of
# the first ones (the plan, the mirror, the changesets); the last ones are the owner's, by hand.
STEPS = {"gmail": ("plan", "labels", "archive", "old_rules", "retire"),
         "graph": ("plan", "consent", "old_rules", "retire")}
_WAITING = ("planned", "committed", "applying")


def _open_structure_changesets(conn: psycopg.Connection, account: str) -> list[dict]:
    return conn.execute(
        "select c.id, c.title, c.status, c.summary->'will_change' as will_change, c.request->>'op' as op,"
        " c.selection->'structure'->>'target' as target, c.request->'args'->>'label' as label from changeset c"
        " where c.selection ? 'structure' and c.selection->'structure'->>'account' = %s and c.status = any(%s)"
        " order by c.id", (account, list(_WAITING))).fetchall()


def next_size(conn: psycopg.Connection, account: str) -> int | None:
    """How many messages per target the next changesets should take: write-back grows 1 → 10 → 100 →
    all, by the largest structure changeset of the account applied so far (None: all)."""
    done = conn.execute(
        "select max(n) as n from (select count(*) filter (where o.status = 'done') as n from changeset c"
        " join changeset_op o on o.changeset_id = c.id where c.selection ? 'structure'"
        " and c.selection->'structure'->>'account' = %s and c.status = 'done' group by c.id) x", (account,)).fetchone()["n"] or 0
    for step in (1, 10, 100):
        if done < step:
            return step
    return None


def checklist(conn: psycopg.Connection, rules: Rules | None = None) -> list[dict]:
    """Per planned account, the steps that make the plan real, each with its live state: done or
    not, the counts, and the changesets waiting for the owner's review. Reads only."""
    rules = rules or load()
    out = []
    providers = {r["id"]: r for r in conn.execute("select id, provider, settings from account")}
    for acc, a in rules.accounts.items():
        prov = providers.get(acc)
        if not prov:
            continue
        run = conn.execute("select started_at, rules_version, rules_sha, placed from structure_run"
                           " where account_id = %s and mode = 'full' order by id desc limit 1", (acc,)).fetchone()
        planned = conn.execute("select count(*)::int as n from structure_plan where account_id = %s", (acc,)).fetchone()["n"]
        steps: dict[str, dict] = {"plan": {
            "done": bool(run and planned), "at": run["started_at"] if run else None, "count": planned,
            "rules_version": run["rules_version"] if run else None,
            "stale": bool(run and run["rules_sha"] != rules.sha)}}
        waiting = _open_structure_changesets(conn, acc)
        if prov["provider"] == "gmail":
            targets = conn.execute(
                "select p.target, count(*)::int as planned,"
                " count(*) filter (where s.on_server)::int as on_server,"
                " count(*) filter (where not s.on_server and s.waiting)::int as waiting"
                " from structure_plan p cross join lateral (select"
                "   exists (select 1 from message_location l where l.message_id = p.message_id and l.present"
                "           and p.target = any(l.labels)) as on_server,"
                "   exists (select 1 from changeset_op o join changeset c on c.id = o.changeset_id"
                "           where o.message_id = p.message_id and o.op = 'add_label' and o.args->>'label' = p.target"
                "           and o.status = 'pending' and c.status = any(%(w)s)) as waiting) s"
                " where p.account_id = %(a)s and p.place = 'label' group by p.target",
                {"a": acc, "w": list(_WAITING)}).fetchall()
            order = {t.path: i for i, t in enumerate(a.targets)}
            targets.sort(key=lambda t: (order.get(t["target"], 999), t["target"]))
            for t in targets:
                t["to_prepare"] = t["planned"] - t["on_server"] - t["waiting"]
                t["changesets"] = [c["id"] for c in waiting if c["op"] == "add_label" and c["label"] == t["target"]]
            lab = {k: sum(t[k] for t in targets) for k in ("planned", "on_server", "waiting", "to_prepare")}
            steps["labels"] = {"done": bool(planned) and lab["on_server"] >= lab["planned"], **lab, "targets": targets,
                               "changesets": [c for c in waiting if c["op"] == "add_label"]}
            arc = conn.execute(
                "select count(*)::int as planned, count(*) filter (where s.inbox)::int as in_inbox,"
                " count(*) filter (where s.inbox and s.labelled)::int as ready,"
                " count(*) filter (where s.inbox and s.waiting)::int as waiting"
                " from structure_plan p cross join lateral (select"
                "   exists (select 1 from message_location l where l.message_id = p.message_id and l.present"
                "           and '\\Inbox' = any(l.labels)) as inbox,"
                "   exists (select 1 from message_location l where l.message_id = p.message_id and l.present"
                "           and p.target = any(l.labels)) as labelled,"
                "   exists (select 1 from changeset_op o join changeset c on c.id = o.changeset_id"
                "           where o.message_id = p.message_id and o.op = 'archive' and o.status = 'pending'"
                "           and c.status = any(%(w)s)) as waiting) s"
                " where p.account_id = %(a)s and p.leaves_inbox and p.place = 'label'",
                {"a": acc, "w": list(_WAITING)}).fetchone()
            steps["archive"] = {"done": bool(planned) and arc["in_inbox"] == 0, **arc,
                                "left": arc["planned"] - arc["in_inbox"],
                                "to_prepare": max(0, arc["ready"] - arc["waiting"]),
                                "changesets": [c for c in waiting if c["op"] == "archive"]}
        else:
            write = bool((prov["settings"] or {}).get("writeback_enabled"))
            steps["consent"] = {"done": write, "manual": True}
        summary = (conn.execute("select summary from structure_run where account_id = %s and mode = 'full'"
                                " order by id desc limit 1", (acc,)).fetchone() or {}).get("summary") or {}
        olds = [o for o in summary.get("old", []) if o.get("count")]
        steps["old_rules"] = {"done": None, "manual": True}
        steps["retire"] = {"done": None, "manual": True, "count": len(olds),
                           "messages": sum(o["count"] for o in olds)}
        can = prov["provider"] == "gmail" and bool(planned) and (
            steps["labels"]["to_prepare"] > 0 or steps["archive"]["to_prepare"] > 0)
        out.append({"account": acc, "name": a.name, "provider": prov["provider"],
                    "steps": [{"id": s, **steps[s]} for s in STEPS.get(prov["provider"], ("plan", "old_rules", "retire"))],
                    "waiting": waiting, "can_prepare": can,
                    "next_size": next_size(conn, acc) if prov["provider"] == "gmail" else None})
    return out


# ---------------------------------------------------------------------------- after a sync

def after_sync(conn: psycopg.Connection, *, now: datetime | None = None) -> dict | None:
    """Place new mail in the plan of every account that has one. Never raises: a sync is never
    failed by the planner. Makes no changeset; the daily one is the owner's to prepare
    (talos structure changesets --new-since …)."""
    try:
        accounts = [r["account_id"] for r in conn.execute(
            "select distinct account_id from structure_run where mode = 'full' order by 1")]
        if not accounts:
            return None
        rules = load()
        out = {}
        for acc in accounts:
            if acc not in rules.accounts:
                continue
            out[acc] = refresh(conn, rules, account=acc, now=now)
        return out
    except Exception:
        log.exception("structure: placing new mail failed; the sync is not affected")
        return None


def refresh_messages(conn: psycopg.Connection, ids: list[int], *, now: datetime | None = None) -> dict | None:
    """Place these messages again, in every account that has a plan (after their values changed:
    talos.incremental accepts new mail's values after the sync placed it). The same incremental
    step as after_sync, for exactly these messages. Never raises."""
    try:
        if not ids:
            return None
        accounts = [r["account_id"] for r in conn.execute(
            "select distinct account_id from structure_run where mode = 'full' order by 1")]
        if not accounts:
            return None
        rules = load()
        by_acc: dict[str, list[int]] = {}
        for r in conn.execute("select id, account_id from message where id = any(%s) and medium = 'email'",
                              (list(ids),)):
            by_acc.setdefault(r["account_id"], []).append(r["id"])
        out = {}
        for acc in accounts:
            if acc in rules.accounts and by_acc.get(acc):
                out[acc] = _store(conn, rules, acc, mode="incremental", ids=by_acc[acc], now=now)
        return out
    except Exception:
        conn.rollback()
        log.exception("structure: placing enriched mail again failed; nothing else is affected")
        return None


def parse_since(text: str, *, now: datetime | None = None) -> datetime:
    """'2026-09-25', '2026-09-25T06:00', or '1d' / '12h' back from now."""
    now = now or datetime.now(timezone.utc)
    t = text.strip()
    if t[-1:] in ("d", "h") and t[:-1].isdigit():
        return now - timedelta(**{"days" if t[-1] == "d" else "hours": int(t[:-1])})
    try:
        d = datetime.fromisoformat(t)
    except ValueError as exc:
        raise StructureError(f"--new-since takes a date (2026-09-25), a time, or 1d / 12h: {text!r}") from exc
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
