"""Machine-mail clusters, for cleaning up (Operations › Clusters).

A cluster is one sender (and, for a shared sender, one system: gold's sender groups,
gold.uncertain_units, the [tag], trailing status word or first word of the subject) in one account,
over incoming machine mail. Machine mail is the effective sender_kind machine, else a machine
origin; with neither, gold's machine signals (the automated flag or an extracted event).

Per cluster: how much mail, first and last seen, the last 90 days, its kinds and types, where it
sits now (Gmail labels, Exchange folders, as mirrored), where the structure plan puts it, and a
status:

- dead: nothing in the last 12 months;
- quiet: under one a month over the last 12 months;
- active: the rest.

The page is sorted dead first by size. The clusters are computed in one pass (about 5 s on the real
archive) and kept in insight_cache, computed again behind the page when mail or values change.

**Cleanup** (`prepare_cleanup`) makes *planned* changesets for one cluster and never commits them:

- Gmail, archive (recommended for a dead system): add the cluster's Talos/Automated/… label (the
  structure plan's Automated place for the message, else Talos/Automated/Old systems), then archive
  what is in the inbox. Apply the label changeset first, so nothing leaves the inbox before it can
  be found under its label.
- Gmail, trash: one trash changeset. Gmail empties its trash after 30 days; the vault keeps the
  original regardless, and the mail stays searchable in Talos.
- Microsoft 365: refused, with what it would do. Its write-back is switched on only for one agreed use
  at a time and off afterwards (docs/writeback-test-plan.md), so this page never makes a changeset
  for it: it is planned only.

Messages already in an open changeset for the same operation are left out. Nothing here talks to
a server: changesets.apply() does, for a committed changeset, and only with write-back enabled.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

from talos import changesets as cs
from talos import gold, insight, structure

NAME = "clusters"
CLUSTER_MIN = 20              # smaller clusters are counted, not listed
DEAD_DAYS = 365
QUIET_PER_YEAR = 12           # under one a month
RECENT_DAYS = 90
ACTIONS = ("archive", "trash")
AUTOMATED = "Talos/Automated/"
CLEANUP_LABEL = "Talos/Automated/Old systems"
PERSON_ORIGINS = ("person", "person_via_system", "list")
NOT_A_PLACE = structure.NOT_OLD
STATUS_ORDER = {"dead": 0, "quiet": 1, "active": 2}
CONSENT = ("Microsoft 365 write-back is switched on only for an agreed use, and off afterwards"
           " (docs/writeback-test-plan.md). Here Microsoft 365 is planned only: no changeset is made.")
TRASH_NOTE = ("Trash in Gmail empties itself after 30 days. Talos's vault keeps the original anyway, and the mail"
              " stays searchable in Talos.")


class ClusterError(ValueError):
    pass


class Refused(ClusterError):
    """Refused for the account (Microsoft 365: planned only); carries what it would have done."""

    def __init__(self, message: str, would: dict):
        super().__init__(message)
        self.would = would


# Incoming machine email with its effective kind and type, and its planned place. The sender side
# ranks human > rule > accepted model > import, the message's own before its thread's.
_ROWS_SQL = """
with base as materialized (
    select m.id, m.account_id, m.thread_id, m.received_at, lower(m.from_address) as sender, m.from_address,
           m.from_name, m.subject, m.is_automated
    from message m where m.medium = 'email' and m.direction = 'in' and m.from_address is not null{only}),
c as (
    select b.id as mid, a.dimension_id as dim, a.value,
           array[case a.source_kind when 'human' then 4 when 'rule' then 3 when 'model' then 2 else 1 end,
                 (a.entity_id = b.id)::int, extract(epoch from a.created_at)::bigint, a.id] as rank
    from base b join assignment a on a.entity_id = any(array[b.id, b.thread_id]) and a.status = 'active'
                                 and a.dimension_id in ('sender_kind', 'origin', 'kind', 'type')),
v as (select mid, (array_agg(value order by rank desc) filter (where dim = 'sender_kind'))[1] as sender_kind,
             (array_agg(value order by rank desc) filter (where dim = 'origin'))[1] as origin,
             (array_agg(value order by rank desc) filter (where dim = 'kind'))[1] as kind,
             (array_agg(value order by rank desc) filter (where dim = 'type'))[1] as type
      from c group by mid)
select b.id, b.account_id, b.received_at, b.sender, b.from_address, b.from_name, b.subject, v.kind, v.type,
       p.target
from base b left join v on v.mid = b.id left join structure_plan p on p.message_id = b.id
where coalesce(v.sender_kind, case when v.origin = any(%(people)s) then 'people' when v.origin is not null
                                   then 'machine' end) = 'machine'
   or (v.sender_kind is null and v.origin is null
       and (b.is_automated or exists (select 1 from event e where e.message_id = b.id)))"""


def _rows(conn: psycopg.Connection, *, account: str | None = None, sender: str | None = None) -> list[dict]:
    only, p = "", {"people": list(PERSON_ORIGINS)}
    if account:
        only += " and m.account_id = %(account)s"
        p["account"] = account
    if sender:
        only += " and lower(m.from_address) = %(sender)s"
        p["sender"] = sender
    return conn.execute(_ROWS_SQL.format(only=only), p).fetchall()


def _inbox_rules() -> dict[str, tuple[set, set]]:
    """Per account, the labels and folders that are its inbox (rules/structure.json)."""
    try:
        r = structure.load()
        return {a: (set(x.inbox_labels), set(x.inbox_folders)) for a, x in r.accounts.items()}
    except structure.StructureError:
        return {}


def _locations(conn: psycopg.Connection, ids: list[int] | None = None) -> dict[int, dict]:
    """Where each message sits on the server now: its places (Gmail labels, else folders) and
    whether it is in the inbox."""
    inbox = _inbox_rules()
    where = " and l.message_id = any(%s)" if ids is not None else ""
    out: dict[int, dict] = {}
    for r in conn.execute("select l.message_id, l.account_id, array_agg(distinct coalesce(l.folder_path, l.folder)) as folders,"
                          " array_agg(distinct x) filter (where x is not null) as labels"
                          " from message_location l left join lateral unnest(l.labels) x on true"
                          f" where l.present{where} group by 1, 2", (ids,) if ids is not None else ()):
        labs, fols = inbox.get(r["account_id"], (set(), set()))
        labels, folders = r["labels"] or [], r["folders"] or []
        places = [x for x in labels + folders if x not in NOT_A_PLACE and x != "[all]"]
        out[r["message_id"]] = {"account": r["account_id"], "places": places,
                                "in_inbox": bool(labs.intersection(labels) or fols.intersection(folders))}
    return out


def status_of(last: datetime | None, last_year: int, now: datetime) -> str:
    """dead: nothing in 12 months; quiet: under one a month over them; active: the rest."""
    if last is None or last < now - timedelta(days=DEAD_DAYS):
        return "dead"
    if last_year < QUIET_PER_YEAR:
        return "quiet"
    return "active"


def _group(rows: list[dict]) -> list[dict]:
    """Clusters of rows: per account, gold's sender groups (a shared sender split by system)."""
    by_acc: dict[str, list] = defaultdict(list)
    for r in rows:
        by_acc[r["account_id"]].append(r)
    out = []
    for acc, rs in by_acc.items():
        by_id = {r["id"]: r for r in rs}
        units = gold.uncertain_units([{"id": r["id"], "stage": "machine", "sender": r["sender"], "subject": r["subject"],
                                       "member_ids": [r["id"]]} for r in rs], group_min=1)
        for g in units["group"]:
            out.append({"account": acc, "sender": g["sender"], "system": g["system"],
                        "rows": [by_id[i] for i in g["cases"]]})
    return out


def key_of(account: str, sender: str, system: str | None) -> str:
    return f"{account}|{sender}|{system or ''}"


def _parse(key: str) -> tuple[str, str, str | None]:
    parts = (key or "").split("|", 2)
    if len(parts) != 3 or not parts[0] or not parts[1]:
        raise ClusterError("a cluster key is account|sender|system")
    return parts[0], parts[1], parts[2] or None


def compute(conn: psycopg.Connection, *, now: datetime | None = None) -> dict:
    """Every cluster of CLUSTER_MIN messages or more, and the summary. Plain SELECTs."""
    now = now or datetime.now(timezone.utc)
    t0 = time.monotonic()
    rows = _rows(conn)
    t_rows = time.monotonic() - t0
    locs = _locations(conn)
    t_locs = time.monotonic() - t0 - t_rows
    year, recent = now - timedelta(days=DEAD_DAYS), now - timedelta(days=RECENT_DAYS)
    clusters, small = [], Counter()
    for g in _group(rows):
        rs = g["rows"]
        if len(rs) < CLUSTER_MIN:
            small["clusters"] += 1
            small["messages"] += len(rs)
            continue
        times = [r["received_at"] for r in rs if r["received_at"]]
        last_year = sum(1 for t in times if t >= year)
        places, plan = Counter(), Counter()
        in_inbox = 0
        for r in rs:
            loc = locs.get(r["id"])
            if loc:
                places.update(loc["places"])
                in_inbox += loc["in_inbox"]
            else:
                places["(no longer on the server)"] += 1
            plan[r["target"] or "(not planned)"] += 1
        newest = sorted(rs, key=lambda r: (r["received_at"] or now, r["id"]), reverse=True)
        subjects, seen = [], set()
        for r in newest:
            s = gold._REPLY.sub("", r["subject"] or "").strip()
            if s not in seen:
                seen.add(s)
                subjects.append(r["subject"])
            if len(subjects) == 2:
                break
        names = Counter(r["from_name"] for r in rs if r["from_name"])
        last = max(times) if times else None
        clusters.append({
            "key": key_of(g["account"], g["sender"], g["system"]), "account": g["account"], "sender": g["sender"],
            "system": g["system"], "name": names.most_common(1)[0][0] if names else None, "count": len(rs),
            "first": min(times) if times else None, "last": last,
            "recent": sum(1 for t in times if t >= recent), "last_year": last_year,
            "status": status_of(last, last_year, now),
            "kinds": Counter(r["kind"] or "(none)" for r in rs).most_common(4),
            "types": Counter(r["type"] or "(none)" for r in rs).most_common(3),
            "places": places.most_common(3), "plan": plan.most_common(3), "in_inbox": in_inbox,
            "examples": subjects})
    clusters.sort(key=lambda c: (STATUS_ORDER[c["status"]], -c["count"], c["key"]))
    t_group = time.monotonic() - t0 - t_rows - t_locs
    return {"clusters": clusters, "small": dict(small), "summary": _summary(clusters, locs), "machine": len(rows),
            "now": now, "timings": {"rows": round(t_rows, 2), "locations": round(t_locs, 2),
                                    "grouping": round(t_group, 2)}}


def _summary(clusters: list[dict], locs: dict[int, dict]) -> dict:
    """Per account: the dead clusters and their mail, the inbox now and after cleaning them, and the
    places (labels, folders) that would lose the most."""
    inbox, place_n = Counter(), defaultdict(Counter)
    for loc in locs.values():
        inbox[loc["account"]] += loc["in_inbox"]
        place_n[loc["account"]].update(loc["places"])
    out = {}
    accounts = sorted({c["account"] for c in clusters})
    for acc in accounts:
        dead = [c for c in clusters if c["account"] == acc and c["status"] == "dead"]
        n = sum(c["count"] for c in dead)
        dead_inbox = sum(c["in_inbox"] for c in dead)
        lose = Counter()
        for c in dead:
            lose.update(dict(c["places"]))
        out[acc] = {"dead_clusters": len(dead), "dead_messages": n, "dead_in_inbox": dead_inbox,
                    "inbox_now": inbox[acc], "inbox_after": inbox[acc] - dead_inbox,
                    "places": [{"place": p, "now": place_n[acc][p], "dead": k}
                               for p, k in lose.most_common(6) if p in place_n[acc]]}
    total = {"dead_clusters": sum(v["dead_clusters"] for v in out.values()),
             "dead_messages": sum(v["dead_messages"] for v in out.values()),
             "dead_in_inbox": sum(v["dead_in_inbox"] for v in out.values()),
             "quiet_clusters": sum(1 for c in clusters if c["status"] == "quiet"),
             "active_clusters": sum(1 for c in clusters if c["status"] == "active")}
    return {"accounts": out, **total}


def fingerprint(conn: psycopg.Connection) -> str:
    r = conn.execute("select (select coalesce(max(id), 0) from message) as m,"
                     " (select coalesce(max(id), 0) from assignment) as a, current_date as d").fetchone()
    return insight.fingerprint([r, insight.table_writes(conn, ["assignment", "message_location", "structure_plan"])])


def state(conn: psycopg.Connection, *, dsn: str | None = None, refresh: bool = False) -> dict:
    row = insight.get(conn, NAME, fingerprint, compute, dsn=dsn, refresh=refresh)
    providers = {r["id"]: r["provider"] for r in conn.execute("select id, provider from account")}
    return {**row["payload"], "computed_at": row["computed_at"], "seconds": row["seconds"], "stale": row["stale"],
            "refreshing": row["refreshing"], "providers": providers, "cleanup_label": CLEANUP_LABEL,
            "consent": CONSENT, "trash_note": TRASH_NOTE}


def members(conn: psycopg.Connection, key: str) -> list[dict]:
    """The cluster's messages now, newest first (its sender's machine mail, and its system when the
    sender is split)."""
    acc, sender, system = _parse(key)
    rows = _rows(conn, account=acc, sender=sender)
    keep = [g for g in _group(rows) if g["system"] == system]
    out = keep[0]["rows"] if keep else []
    return sorted(out, key=lambda r: (r["received_at"] or datetime.min.replace(tzinfo=timezone.utc), r["id"]), reverse=True)


def examples(conn: psycopg.Connection, key: str, *, limit: int = 25, offset: int = 0) -> dict:
    rows = members(conn, key)
    page = rows[offset:offset + limit]
    return {"key": key, "total": len(rows), "rows": [
        {k: r[k] for k in ("id", "account_id", "received_at", "from_name", "from_address", "subject", "kind", "type",
                           "target")} for r in page]}


_OPEN_OP = ("select o.message_id from changeset_op o join changeset c on c.id = o.changeset_id"
            " where o.message_id = any(%s) and o.op = %s and o.args = %s and o.status = 'pending'"
            " and c.status in ('planned', 'committed', 'applying')")


def _free(conn: psycopg.Connection, ids: list[int], op: str, args: dict) -> list[int]:
    taken = {r["message_id"] for r in conn.execute(_OPEN_OP, (ids, op, Jsonb(args)))}
    return [i for i in ids if i not in taken]


def prepare_cleanup(conn: psycopg.Connection, key: str, action: str) -> dict:
    """Planned changesets for one cluster (see the module docstring). Never committed."""
    if action not in ACTIONS:
        raise ClusterError(f"the action is {' or '.join(ACTIONS)}")
    acc, sender, system = _parse(key)
    prov = conn.execute("select provider from account where id = %s", (acc,)).fetchone()
    if not prov:
        raise ClusterError(f"no account {acc!r}")
    rows = members(conn, key)
    if not rows:
        raise ClusterError("the cluster has no mail now")
    locs = _locations(conn, [r["id"] for r in rows])
    present = [r for r in rows if r["id"] in locs]
    in_inbox = [r["id"] for r in present if locs[r["id"]]["in_inbox"]]
    would = {"account": acc, "action": action, "messages": len(rows), "on_server": len(present),
             "in_inbox": len(in_inbox)}
    if prov["provider"] == "graph":
        raise Refused(f"{acc}: {CONSENT} It would {action} {len(present):,} messages"
                      f" ({len(in_inbox):,} in the inbox).", would)
    if prov["provider"] != "gmail":
        raise ClusterError(f"{acc} is a {prov['provider']} account: Talos cleans up Gmail only, for now")
    title_of = system and f"{sender} · {system}" or sender
    made = []
    marker = {"cleanup": {"cluster": key, "account": acc, "action": action}}
    if action == "trash":
        ids = _free(conn, [r["id"] for r in present], "trash", {})
        if ids:
            made.append(_make(conn, "trash", {}, ids, marker, f"Cleanup: trash {{n}} from {title_of}", TRASH_NOTE))
    else:
        by_label: dict[str, list[int]] = defaultdict(list)
        for r in present:
            t = r["target"] or ""
            by_label[t if t.startswith(AUTOMATED) else CLEANUP_LABEL].append(r["id"])
        for label, ids in sorted(by_label.items(), key=lambda kv: -len(kv[1])):
            ids = _free(conn, ids, "add_label", {"label": label})
            if ids:
                made.append(_make(conn, "add_label", {"label": label}, ids, marker,
                                  f"Cleanup: add {label} to {{n}} from {title_of}",
                                  "Apply this before the archive changeset, so nothing leaves the inbox before it"
                                  " can be found under its label."))
        ids = _free(conn, in_inbox, "archive", {})
        if ids:
            made.append(_make(conn, "archive", {}, ids, marker, f"Cleanup: archive {{n}} from {title_of}",
                              "Apply after the label changeset. The mail leaves the inbox only; it stays in"
                              " All Mail, under its label, and searchable in Talos."))
    return {**would, "changesets": made, "status": "planned",
            "note": "Planned only: dry-run and commit each on the Changesets page / talos changeset commit ID --max N."
                    + (" " + TRASH_NOTE if action == "trash" else "")}


def _make(conn: psycopg.Connection, op: str, args: dict, ids: list[int], marker: dict, title: str, note: str) -> dict:
    with conn.transaction():
        cid = cs.create(conn, "Cleanup (planning)", op, args=args, message_ids=ids)
        conn.execute("update changeset set selection = selection || %s, note = %s where id = %s",
                     (Jsonb(marker), "Made on the Clusters page. " + note + " Review it, dry-run it, then commit"
                      " with --max in steps (1, 10, 100, all) and apply.", cid))
        summary = cs.plan(conn, cid)
        n = summary["will_change"]
        t = title.format(n=f"{n:,} {'message' if n == 1 else 'messages'}")
        conn.execute("update changeset set title = %s where id = %s", (t, cid))
    return {"id": cid, "title": t, "op": op, "args": args, "selected": len(ids), "will_change": n,
            "skipped_because": summary["skipped_because"], "status": "planned"}
