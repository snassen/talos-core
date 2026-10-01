"""Rule suggestions: rules drafted from the values the owner (or Jev, accepted) has already given.

Where at least MIN_MESSAGES incoming mails from one sender carry the same accepted value of a
field, and none of that sender's mail carries a different accepted one, a rule is drafted:

    {"field": "from_address", "op": "is", "value": <sender>}  →  {"dimension": <field>, "value": <value>}

When the sender as a whole conflicts (its mail has two values of the field), its subject patterns
(message_pattern: sender + subject skeleton) are tried the same way, and the draft adds a subject
condition that matches that skeleton ({"field": "subject", "op": "matches", "value": <regex>}).

An accepted value is the value that counts for the message (human > rule > accepted model, its own
before its thread's, as effective_message_assignment ranks them) when it was given by the owner or accepted
from Jev. A message whose value a rule already sets does not count: it is covered.

Nothing is created here. draft() returns suggestions, always switched off; add() saves one as a rule
that stays off until the owner switches it on. The suggestions are grouped by the value's family (topic: Work,
Family & home…) or, for a field with few values (kind: Alert, Report, Offer…), by the value itself.

**The drill-down** (tree): field → category → value → the suggestions (a sender, or a sender and a
subject pattern), each level with its number of suggestions and the messages they would cover. Within
one field a sender's suggestions never overlap (a sender is suggested whole, or else by disjoint subject
patterns), so a level's coverage is the sum of its members' and equals their union.

**A ticked value** becomes ONE broader rule (group_rule): an OR over its member suggestions,
`from_address in [senders]` plus a (sender, subject pattern) pair per patterned member, saved off, named
like "Topic Work/Backup: 34 senders". A ticked category with several values becomes one such rule per
value (a rule sets one value). group_preview() says, before anything is saved, what it would match and
how many of those messages carry another accepted value. The members of a group rule are covered and no
longer suggested; they come back when the rule is removed. So does a single suggestion's rule.

On the real archive the drafting is one grouped pass over the accepted values (a few seconds); the
web app caches it, with each suggestion's coverage, until the assignments change (Cache).
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import psycopg

from talos import rules

MIN_MESSAGES = 20
DIMS = ("kind", "topic", "value", "origin", "keep", "sphere")
PRIORITY = 200            # after the hand-made rules (rules/*.json): for a one-value field the first match wins
BY_VALUE_UP_TO = 12       # a field with at most this many values is grouped by value, else by family
EXAMPLES = 3

# Reply and forward prefixes, as subject_skeleton() strips them (sql/010_enrich_prepass.sql).
_REPLY = r"(re|sv|fw|fwd|vb|aw|wg|tr|rv|vs|antw)\s*(\[[0-9]+\]|\([0-9]+\))?\s*:"
_P = r"[^[:alnum:]#[:space:]]"   # the punctuation the skeleton drops ('#' it keeps)


def skeleton_regex(skeleton: str) -> str:
    """A PostgreSQL regular expression (for ~*, as the rule 'matches' runs it) that matches a subject
    exactly when its subject_skeleton() is this skeleton.

    The skeleton is the lower-cased subject without its reply prefixes, every token holding a digit
    turned into '#' (a run of them into one), punctuation dropped, and the first three words kept.
    Dropping punctuation keeps the whitespace around it, so the skeleton's spaces are matched one for
    one, with dropped punctuation allowed between them and around and inside each word. A skeleton of
    fewer than three words must reach the end of the subject."""
    parts = re.split(r"(\s+)", skeleton.strip())
    hashtok = rf"(\S*[0-9]\S*|{_P}*#{_P}*)"
    out = []
    words = 0
    for part in parts:
        if not part:
            continue
        if part.isspace():
            out.append(r"\s" + rf"({_P}*\s)" * (len(part) - 1) if len(part) > 1 else r"\s")
        elif part == "#":
            words += 1
            out.append(rf"{hashtok}(\s({_P}*\s)*{hashtok})*")
        else:
            words += 1
            inner = f"{_P}*".join(ch if ch.isalnum() else re.escape(ch) for ch in part)
            out.append(f"{_P}*{inner}{_P}*")
    tail = r"(\s|$)" if words >= 3 else rf"[[:space:]]*({_P}+[[:space:]]*)*$"
    return rf"^\s*({_REPLY}\s*)*(?!\s*{_REPLY})[[:space:]]*({_P}+[[:space:]]*)*{''.join(out)}{tail}"


def _slug(text: str, n: int) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n].strip("-")


def rule_id(dimension: str, value: str, sender: str, skeleton: str | None) -> str:
    """A stable id, so a suggestion is the same one every time it is drafted."""
    h = hashlib.sha1(f"{dimension}|{value}|{sender}|{skeleton or ''}".encode()).hexdigest()[:6]
    base = f"suggest-{dimension}-{_slug(sender, 40)}" + (f"-{_slug(skeleton, 16)}" if skeleton else "")
    return f"{base[:72].strip('-')}-{h}"


def _meta(conn: psycopg.Connection) -> dict[str, dict]:
    """Per field: its label, allowed values, and each value's label and family."""
    out = {}
    for d in conn.execute("select id, label, allowed, value_meta from dimension where id = any(%s)", (list(DIMS),)):
        out[d["id"]] = {"label": d["label"], "allowed": d["allowed"], "meta": d["value_meta"] or {}}
    return out


def _group(meta: dict, dimension: str, value: str) -> tuple[str, str]:
    """(category key, category name) of a suggestion: the value's family, or the value itself for a
    field with few values."""
    m = meta.get(dimension) or {}
    vm = (m.get("meta") or {}).get(value) or {}
    allowed = m.get("allowed") or list((m.get("meta") or {}).keys())
    label = vm.get("label") or value
    if allowed and len(allowed) <= BY_VALUE_UP_TO or not vm.get("family"):
        return f"{dimension}:{value}", label
    return f"{dimension}:{vm['family']}", vm["family"]


_ACCEPTED_SQL = """
with base as materialized (
    select m.id, m.thread_id, m.from_address as sender, mp.pattern_key, mp.skeleton
    from message m left join message_pattern mp on mp.message_id = m.id
    where m.medium = 'email' and m.direction = 'in' and coalesce(m.from_address, '') <> ''
      and not exists (select 1 from my_address x where lower(x.address) = m.from_address)
      and m.from_address in (select from_address from message where medium = 'email' and direction = 'in'
                             group by 1 having count(*) >= %(min)s)),
ranked as (
    select b.id, a.dimension_id as dim, a.value, a.source_kind,
           row_number() over (partition by b.id, a.dimension_id
                              order by case a.source_kind when 'human' then 4 when 'rule' then 3
                                                          when 'model' then 2 else 1 end desc,
                                       (a.entity_id = b.id) desc, a.created_at desc, a.id desc) as rn
    from base b join assignment a on a.entity_id = any(array[b.id, b.thread_id]) and a.status = 'active'
                                 and a.dimension_id = any(%(dims)s)),
acc as materialized (
    select b.sender, b.pattern_key, b.skeleton, r.dim, r.value
    from ranked r join base b on b.id = r.id
    where r.rn = 1 and r.source_kind in ('human', 'model')),
s as (select sender, dim, count(*)::int as n, count(distinct value)::int as nv, min(value) as value
      from acc group by 1, 2),
p as (select sender, pattern_key, min(skeleton) as skeleton, dim, count(*)::int as n,
             count(distinct value)::int as nv, min(value) as value
      from acc where pattern_key is not null group by 1, 2, 4)
select s.sender, null::text as skeleton, s.dim, s.value, s.n from s where s.n >= %(min)s and s.nv = 1
union all
select p.sender, p.skeleton, p.dim, p.value, p.n from p join s using (sender, dim)
where s.nv > 1 and p.n >= %(min)s and p.nv = 1 and p.skeleton <> ''
order by 3, 4, 5 desc, 1, 2
"""


def draft(conn: psycopg.Connection, *, min_messages: int = MIN_MESSAGES) -> list[dict]:
    """Every suggestion, drafted from the accepted values, off, without its preview count or examples
    (details() adds those for the few shown). Read-only."""
    meta = _meta(conn)
    dims = [d for d in DIMS if d in meta]
    conn.execute("set local work_mem = '256MB'")
    rows = conn.execute(_ACCEPTED_SQL, {"min": min_messages, "dims": dims}).fetchall()
    out = []
    for r in rows:
        dim, value, sender, skel = r["dim"], r["value"], r["sender"], r["skeleton"]
        allowed = meta[dim]["allowed"]
        if allowed and value not in allowed:  # a retired value: a rule could not be saved with it
            continue
        conditions = [{"field": "from_address", "op": "is", "value": sender}]
        if skel:
            conditions.append({"field": "subject", "op": "matches", "value": skeleton_regex(skel)})
        key, name = _group(meta, dim, value)
        vlabel = ((meta[dim]["meta"] or {}).get(value) or {}).get("label") or value
        dlabel = meta[dim]["label"]
        out.append({
            "id": rule_id(dim, value, sender, skel), "sender": sender, "skeleton": skel,
            "dimension": dim, "dimension_label": dlabel, "value": value, "value_label": vlabel,
            "category": key, "category_name": name, "agree": r["n"],
            "name": f"{sender}{' “' + skel + '…”' if skel else ''} → {dlabel}: {vlabel}",
            "conditions": conditions, "action": {"dimension": dim, "value": value},
            "priority": PRIORITY, "enabled": False})
    return out


def without_existing(conn: psycopg.Connection, suggestions: list[dict]) -> list[dict]:
    """Leave out a suggestion that is a rule already (by id, or by the same conditions and field), or a
    member of a group rule. A removed rule is not in the rule table, so its suggestions come back."""
    have = conn.execute("select id, conditions, action, origin from rule").fetchall()
    ids = {r["id"] for r in have}
    for r in have:
        ids.update(m["id"] for m in (((r["origin"] or {}).get("group") or {}).get("members") or []))
    same = {(repr(r["conditions"]), (r["action"] or {}).get("dimension")) for r in have}
    return [s for s in suggestions if s["id"] not in ids and (repr(s["conditions"]), s["dimension"]) not in same]


def categories(suggestions: list[dict]) -> list[dict]:
    """The suggestions by field, then category, with counts; largest first within a field."""
    by: dict[str, dict] = {}
    for s in suggestions:
        c = by.setdefault(s["category"], {"key": s["category"], "name": s["category_name"], "dimension": s["dimension"],
                                          "dimension_label": s["dimension_label"], "count": 0, "agree": 0})
        c["count"] += 1
        c["agree"] += s["agree"]
    order = {d: i for i, d in enumerate(DIMS)}
    return sorted(by.values(), key=lambda c: (order.get(c["dimension"], 99), -c["count"], c["name"]))


def details(conn: psycopg.Connection, suggestions: list[dict]) -> list[dict]:
    """Each suggestion with its preview count (what the rule would match: every message from the
    sender, and the subject pattern) and three recent example subjects. Two queries in all."""
    if not suggestions:
        return []
    senders = [s["sender"] for s in suggestions]
    rx = [s["conditions"][1]["value"] if len(s["conditions"]) > 1 else None for s in suggestions]
    counts = {r["i"]: r["n"] for r in conn.execute(
        "select u.i, count(m.id)::int as n from unnest(%s::text[], %s::text[]) with ordinality as u(sender, rx, i)"
        " join message m on m.from_address = u.sender and (u.rx is null or coalesce(m.subject, '') ~* u.rx)"
        " group by u.i", (senders, rx))}
    examples: dict[int, list[dict]] = {}
    for r in conn.execute(
            "select u.i, x.id, x.subject, x.received_at from unnest(%s::text[], %s::text[]) with ordinality as u(sender, rx, i)"
            " cross join lateral (select m.id, m.subject, m.received_at from message m where m.from_address = u.sender"
            "   and (u.rx is null or coalesce(m.subject, '') ~* u.rx) order by m.received_at desc nulls last limit 12) x",
            (senders, rx)):
        got = examples.setdefault(r["i"], [])
        if len(got) < EXAMPLES and all(e["subject"] != r["subject"] for e in got):
            got.append({"id": r["id"], "subject": r["subject"], "received_at": r["received_at"]})
    return [{**s, "preview": counts.get(i, 0), "examples": examples.get(i, [])} for i, s in enumerate(suggestions, 1)]


def coverage(conn: psycopg.Connection, suggestions: list[dict]) -> dict[str, int]:
    """{suggestion id: the messages its rule would match}. A sender is suggested in several fields, so
    each sender is counted once: whole senders in one grouped pass over the sender index, patterned ones
    by subject_skeleton() in one more (skeleton_regex matches a subject exactly when its skeleton is the
    pattern's, so this is the count the rule's regex gives, without running 400 regexes over 80,000
    subjects: 1 s instead of 15 on the archive)."""
    if not suggestions:
        return {}
    whole = sorted({s["sender"] for s in suggestions if not s.get("skeleton")})
    patterned = sorted({s["sender"] for s in suggestions if s.get("skeleton")})
    n_whole = {r["sender"]: r["n"] for r in conn.execute(
        "select from_address as sender, count(*)::int as n from message where from_address = any(%s) group by 1",
        (whole,))} if whole else {}
    n_skel = {(r["sender"], r["skeleton"]): r["n"] for r in conn.execute(
        "select from_address as sender, subject_skeleton(subject) as skeleton, count(*)::int as n from message"
        " where from_address = any(%s) group by 1, 2", (patterned,))} if patterned else {}
    return {s["id"]: n_skel.get((s["sender"], s["skeleton"]), 0) if s.get("skeleton") else n_whole.get(s["sender"], 0)
            for s in suggestions}


def add(conn: psycopg.Connection, suggestion: dict) -> rules.Rule:
    """Save a suggestion as a rule, switched off. Never runs the rules."""
    today = datetime.now(timezone.utc).date().isoformat()
    what = f"its subjects like “{suggestion['skeleton']}…”" if suggestion.get("skeleton") else "its mail"
    rule = rules.Rule(
        id=suggestion["id"], name=suggestion["name"][:200], conditions=suggestion["conditions"],
        action=suggestion["action"], priority=PRIORITY, enabled=False,
        description=(f"Suggested by Talos on {today}: {suggestion['agree']} messages from {suggestion['sender']} "
                     f"({what}) have the accepted value {suggestion['dimension']}: {suggestion['value']}, and none has "
                     "another. Saved off; switch it on to use it."),
        origin={"suggestion": suggestion["id"], "dimension": suggestion["dimension"], "value": suggestion["value"]})
    return rules.save(conn, rule)


# ---------------------------------------------------------------------------- the drill-down

def _made_from_suggestions(r: dict) -> tuple[str, str, str] | None:
    """(kind, dimension, value) of a rule Talos made from suggestions ('single' or 'group'), else None."""
    o = r.get("origin") or {}
    a = r.get("action") or {}
    if o.get("group"):
        return "group", o["group"]["dimension"], o["group"]["value"]
    if o.get("suggestion") or str(r.get("id", "")).startswith("suggest-"):
        if a.get("dimension") and a.get("value"):
            return "single", a["dimension"], a["value"]
    return None


def tree(conn: psycopg.Connection, suggestions: list[dict], cover: dict[str, int]) -> list[dict]:
    """The drill-down: field → category → value → suggestions, each level with its number of suggestions
    (count), the messages they would cover (coverage) and the messages whose accepted values agree (agree).
    Each value also lists the rules already made from its suggestions (a group rule, single ones), so
    they can be seen and removed where they were made. Largest coverage first at every level."""
    meta = _meta(conn)
    fields: dict[str, dict] = {}

    def node(dim: str, value: str) -> dict:
        m = meta.get(dim) or {}
        f = fields.setdefault(dim, {"dimension": dim, "label": m.get("label") or dim, "count": 0, "coverage": 0,
                                    "agree": 0, "categories": {}})
        key, name = _group(meta, dim, value)
        c = f["categories"].setdefault(key, {"key": key, "name": name, "count": 0, "coverage": 0, "agree": 0,
                                             "values": {}})
        vlabel = (((m.get("meta") or {}).get(value)) or {}).get("label") or value
        return c["values"].setdefault(value, {"value": value, "label": vlabel, "count": 0, "coverage": 0,
                                               "agree": 0, "rules": [], "suggestions": []})

    for s in suggestions:
        v = node(s["dimension"], s["value"])
        n = cover.get(s["id"], 0)
        v["suggestions"].append({"id": s["id"], "sender": s["sender"], "skeleton": s["skeleton"], "agree": s["agree"],
                                 "coverage": n, "name": s["name"]})
        v["count"] += 1
        v["coverage"] += n
        v["agree"] += s["agree"]
    for r in conn.execute("select id, name, version, enabled, action, origin from rule order by id"):
        made = _made_from_suggestions(r)
        if made and made[1] in meta:
            kind, dim, value = made
            members = len(((r["origin"] or {}).get("group") or {}).get("members") or []) if kind == "group" else 1
            node(dim, value)["rules"].append({"id": r["id"], "name": r["name"], "version": r["version"],
                                              "enabled": r["enabled"], "kind": kind, "members": members})
    order = {d: i for i, d in enumerate(DIMS)}
    out = []
    for f in sorted(fields.values(), key=lambda f: order.get(f["dimension"], 99)):
        cats = []
        for c in f["categories"].values():
            vals = sorted(c["values"].values(), key=lambda v: (-v["coverage"], v["label"]))
            for v in vals:
                v["suggestions"].sort(key=lambda x: (-x["coverage"], x["sender"], x["skeleton"] or ""))
                for k in ("count", "coverage", "agree"):
                    c[k] += v[k]
            c["values"] = vals
            c["single"] = len(vals) == 1 and c["key"] == f"{f['dimension']}:{vals[0]['value']}"
            cats.append(c)
        cats.sort(key=lambda c: (-c["coverage"], c["name"]))
        for c in cats:
            for k in ("count", "coverage", "agree"):
                f[k] += c[k]
        f["categories"] = cats
        out.append(f)
    return out


def search(suggestions: list[dict], q: str) -> list[dict]:
    """The suggestions whose sender, subject pattern, value or category holds q (any case)."""
    q = q.strip().lower()
    if not q:
        return suggestions
    return [s for s in suggestions if any(q in (s.get(k) or "").lower()
                                         for k in ("sender", "skeleton", "value", "value_label", "category_name"))]


def group_id(dimension: str, value: str) -> str:
    """The one id of the group rule for a value, so ticking it again finds the rule it made."""
    h = hashlib.sha1(f"group|{dimension}|{value}".encode()).hexdigest()[:6]
    return f"suggest-group-{dimension}-{_slug(value, 40)}-{h}"


def _member(s: dict) -> dict:
    return {"id": s["id"], "sender": s["sender"],
            "rx": s["conditions"][1]["value"] if len(s["conditions"]) > 1 else None, "agree": s["agree"]}


def group_conditions(members: list[dict]) -> list[dict]:
    """An OR over the members: from_address in [every sender suggested whole], and one (sender, subject
    pattern) pair per member suggested by pattern."""
    senders = sorted({m["sender"] for m in members if not m.get("rx")})
    alts: list = [{"field": "from_address", "op": "in", "value": senders}] if senders else []
    for m in sorted((m for m in members if m.get("rx")), key=lambda m: (m["sender"], m["rx"])):
        alts.append([{"field": "from_address", "op": "is", "value": m["sender"]},
                     {"field": "subject", "op": "matches", "value": m["rx"]}])
    if not alts:
        raise rules.RuleError("a group rule needs at least one suggestion")
    if len(alts) == 1 and isinstance(alts[0], dict):
        return alts
    return [{"any": alts}]


def group_rule(conn: psycopg.Connection, suggestions: list[dict], dimension: str, value: str) -> rules.Rule:
    """The one broader rule for a value, from its suggestions (and, if the value has a group rule already,
    that rule's members too): off, priority PRIORITY. Not saved."""
    meta = _meta(conn)
    if dimension not in meta:
        raise rules.RuleError(f"suggestions are not made for {dimension!r}")
    mine = [_member(s) for s in suggestions if s["dimension"] == dimension and s["value"] == value]
    rid = group_id(dimension, value)
    old = conn.execute("select origin from rule where id = %s", (rid,)).fetchone()
    have = {m["id"] for m in mine}
    for m in (((old or {}).get("origin") or {}).get("group") or {}).get("members") or []:
        if m["id"] not in have:
            mine.append(m)
    if not mine:
        raise rules.RuleError(f"no suggestions for {dimension}: {value}")
    mine.sort(key=lambda m: (m["sender"], m.get("rx") or ""))
    dlabel = meta[dimension]["label"]
    vlabel = ((meta[dimension]["meta"] or {}).get(value) or {}).get("label") or value
    senders = len({m["sender"] for m in mine})
    patterns = sum(1 for m in mine if m.get("rx"))
    what = f"{senders} sender{'' if senders == 1 else 's'}" + (f", {patterns} by subject pattern" if patterns else "")
    shown = value if "/" in value else vlabel   # "Topic Work/Backup", "Kind Alert"
    return rules.Rule(
        id=rid, name=f"{dlabel} {shown}: {what}"[:200], conditions=group_conditions(mine),
        action={"dimension": dimension, "value": value}, priority=PRIORITY, enabled=False,
        origin={"group": {"dimension": dimension, "value": value, "members": mine}})


def group_preview(conn: psycopg.Connection, rule: rules.Rule) -> dict:
    """What a group rule would do, before it is saved: the messages it matches, how many of them carry
    another accepted value of the field (the owner's, or accepted from Jev: a conflict), how many the same one,
    and how many have a value from another rule already."""
    where, params = rules.check(conn, rule.conditions)
    dim, value = rule.action["dimension"], rule.action["value"]
    r = conn.execute(f"""
        with hit as materialized (select m.id, m.thread_id from message m where {where}),
        ranked as (
            select h.id, a.value, a.source_kind,
                   row_number() over (partition by h.id
                                      order by case a.source_kind when 'human' then 4 when 'rule' then 3
                                                                  when 'model' then 2 else 1 end desc,
                                               (a.entity_id = h.id) desc, a.created_at desc, a.id desc) as rn
            from hit h join assignment a on a.entity_id = any(array[h.id, h.thread_id]) and a.status = 'active'
                                        and a.dimension_id = %s)
        select (select count(*) from hit)::int as count,
               count(*) filter (where source_kind in ('human', 'model') and value <> %s)::int as conflicts,
               count(*) filter (where source_kind in ('human', 'model') and value = %s)::int as agree,
               count(*) filter (where source_kind not in ('human', 'model'))::int as ruled
        from ranked where rn = 1""", [*params, dim, value, value]).fetchone()
    members = rule.origin["group"]["members"]
    return {"id": rule.id, "name": rule.name, "dimension": dim, "value": value, "conditions": rule.conditions,
            "members": len(members), "senders": len({m["sender"] for m in members}),
            "patterns": sum(1 for m in members if m.get("rx")),
            "exists": conn.execute("select 1 from rule where id = %s", (rule.id,)).fetchone() is not None, **r}


def category_values(suggestions: list[dict], category: str) -> list[tuple[str, str]]:
    """The (dimension, value) pairs a ticked category holds, largest first."""
    n: dict[tuple[str, str], int] = {}
    for s in suggestions:
        if s["category"] == category:
            n[(s["dimension"], s["value"])] = n.get((s["dimension"], s["value"]), 0) + s["agree"]
    return sorted(n, key=lambda k: (-n[k], k))


def add_group(conn: psycopg.Connection, suggestions: list[dict], dimension: str, value: str) -> tuple[rules.Rule, dict]:
    """Save the group rule of a value, off; its description says what it matched when it was made.
    Never runs the rules."""
    rule = group_rule(conn, suggestions, dimension, value)
    p = group_preview(conn, rule)
    today = datetime.now(timezone.utc).date().isoformat()
    agree = sum(m.get("agree") or 0 for m in rule.origin["group"]["members"])
    by_pattern = f", {p['patterns']} by subject pattern" if p["patterns"] else ""
    rule.description = (
        f"Made by Talos on {today} from {p['members']} suggestion{'' if p['members'] == 1 else 's'} for "
        f"{dimension}: {value} ({p['senders']} sender{'' if p['senders'] == 1 else 's'}{by_pattern}); {agree} messages of theirs agree on the "
        f"value. When made it matched {p['count']} messages, {p['conflicts']} of them with another accepted "
        "value. Saved off; switch it on to use it.")
    return rules.save(conn, rule), p


# ---------------------------------------------------------------------------- the web app's cache

@dataclass
class Cache:
    """The drafted suggestions, kept until the accepted values or the mail change.

    Each suggestion's coverage (the messages its rule would match) is counted with the drafting, for
    the drill-down's numbers.

    The signature is the tables' change counters (pg_stat_user_tables) and the newest assignment:
    reading it costs a millisecond, drafting a few seconds. Existing rules are filtered out on every
    request instead, so adding a rule never invalidates it."""
    sig: tuple | None = None
    rows: list[dict] = field(default_factory=list)
    cover: dict[str, int] = field(default_factory=dict)
    at: datetime | None = None
    seconds: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    @staticmethod
    def signature(conn: psycopg.Connection) -> tuple:
        stats = conn.execute(
            "select relname, n_tup_ins + n_tup_upd + n_tup_del as n from pg_stat_user_tables"
            " where relname in ('assignment', 'message', 'message_pattern', 'dimension') order by 1").fetchall()
        top = conn.execute("select max(id) as id from assignment").fetchone()["id"]
        return tuple((r["relname"], r["n"]) for r in stats) + (top,)

    def invalidate(self) -> None:
        """Draft again at the next request: a removed rule's values are gone (the change counters
        behind the signature can lag by a second)."""
        with self.lock:
            self.sig = None

    def get(self, conn: psycopg.Connection) -> list[dict]:
        with self.lock:
            sig = self.signature(conn)
            if sig != self.sig:
                started = time.monotonic()
                with conn.transaction():
                    self.rows = draft(conn)
                    self.cover = coverage(conn, self.rows)
                self.seconds = round(time.monotonic() - started, 2)
                self.at = datetime.now(timezone.utc)
                self.sig = sig
            return self.rows
