"""The work space: everything the owner runs, in one picture (docs/workspace.md).

- ``overview`` is the page: the areas, each with the binders linked to it (projects, systems,
  topics), what needs the owner, and the watchers. A binder is linked to an area by a ``related`` or
  ``member_of`` edge in either direction (the vault import made ``related`` ones).
- A binder's **search terms** (attrs.terms; its name when it has none) are what Talos looks for in
  the mail. ``binder_mail`` counts, per binder, the mail and threads that mention them (all and
  the last 30 days) and, from the threads two binders share, each binder's **neighbours**. It is
  kept in insight_cache: one full-text search per binder, a few seconds in all.
- ``found`` is a binder's "Found in your mail": recent threads that mention its terms and are not
  members, people's first. Adding one is objects.add; "not this" is objects.exclude.
- **Watchers** are saved Messages selections (the view's own query string) that count what matches
  and what arrived since the owner last looked. One may belong to a binder; taking its new mail into
  the binder is the owner's step (``take_in``), never automatic.

Everything here reads the archive and writes Talos's own tables only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlencode

import psycopg

from talos import insight, objects, personal, search, work

MAP = "space-binders"
RECENT_DAYS = 30


class SpaceError(ValueError):
    pass


# ---------------------------------------------------------------- binders and their mail

def terms(obj: dict) -> list[str]:
    """What Talos looks for in the mail for a binder: attrs.terms, or its name."""
    t = [x.strip() for x in (obj.get("attrs") or {}).get("terms") or [] if isinstance(x, str) and x.strip()]
    return t or [obj["name"]]


def set_terms(conn: psycopg.Connection, object_id: int, values: list[str]) -> list[str]:
    obj = objects.get(conn, object_id)
    if not obj:
        raise SpaceError("no such binder")
    clean = []
    for v in values or []:
        v = str(v).strip()
        if v and v.lower() not in [c.lower() for c in clean]:
            clean.append(v[:80])
    if len(clean) > 12:
        raise SpaceError("at most 12 search terms")
    conn.execute("update object set attrs = case when %s::text[] = '{}' then attrs - 'terms'"
                 " else jsonb_set(attrs, '{terms}', to_jsonb(%s::text[])) end, updated_at = now() where id = %s",
                 (clean, clean, object_id))
    return clean or [obj["name"]]


def _term_sql(values: list[str], select: str) -> tuple[str, dict]:
    """The mail that mentions any of the terms, each as a phrase ("Check Point", not check and
    point anywhere): in the text (either dictionary), the subject or the sender's address."""
    parts, params = [], {}
    for i, t in enumerate(values):
        params[f"t{i}"], params[f"l{i}"] = t, f"%{t}%"
        parts.append(f"select x.message_id as id from message_text x where x.search @@"
                     f" (phraseto_tsquery('swedish', %(t{i})s) || phraseto_tsquery('english', %(t{i})s))"
                     f" union select id from message where subject ilike %(l{i})s"
                     f" union select id from message where from_address ilike %(l{i})s")
    return f"select {select} from message m where m.id in ({' union '.join(parts)})", params


def _binders(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("select id, kind, name, attrs from object where not archived and kind <> 'saved_search'"
                        " order by kind, name").fetchall()


def compute_binder_mail(conn: psycopg.Connection) -> dict:
    """Per binder: mail and threads mentioning its terms (all, and the last 30 days); its neighbours
    by shared threads."""
    out, threads = {}, {}
    for b in _binders(conn):
        if b["kind"] == "area":
            continue
        sql, p = _term_sql(terms(b), "m.id, m.thread_id, m.received_at")
        rows = conn.execute(f"select count(*) as n, count(distinct thread_id) as th,"
                            f" count(*) filter (where received_at > now() - make_interval(days => {RECENT_DAYS})) as recent,"
                            f" max(received_at) as last, array_agg(distinct thread_id) filter (where thread_id is not null) as tids"
                            f" from ({sql}) x", p).fetchone()
        threads[b["id"]] = set(rows["tids"] or [])
        out[str(b["id"])] = {"mentions": rows["n"], "threads": rows["th"], "recent": rows["recent"], "last": rows["last"]}
    ids = list(threads)
    for a in ids:
        near = []
        for b in ids:
            if a == b or not threads[a] or not threads[b]:
                continue
            shared = len(threads[a] & threads[b])
            # shared threads that are a real part of both, not one big binder swallowing a small one
            if shared >= 3 and shared / min(len(threads[a]), len(threads[b])) >= 0.02:
                near.append({"id": b, "shared": shared})
        near.sort(key=lambda x: -x["shared"])
        out[str(a)]["near"] = near[:6]
    return {"binders": out, "at": datetime.now(timezone.utc)}


def binder_mail_fingerprint(conn: psycopg.Connection) -> str:
    r = conn.execute("select (select max(id) from message) m,"
                     " (select md5(string_agg(id || ':' || name || ':' || coalesce(attrs->>'terms', '') || archived, ',' order by id))"
                     " from object) o").fetchone()
    return insight.fingerprint([r, datetime.now(timezone.utc).date()])


def binder_mail(conn: psycopg.Connection, *, dsn: str | None = None, refresh: bool = False) -> dict:
    row = insight.get(conn, MAP, binder_mail_fingerprint, compute_binder_mail, dsn=dsn, refresh=refresh,
                      first_behind=bool(dsn))
    payload = row["payload"] or {"binders": {}}
    return {**payload, "stale": row["stale"], "refreshing": row["refreshing"]}


def _links(conn: psycopg.Connection) -> list[dict]:
    """Object-to-object links in either direction (related, member_of), counted once."""
    return conn.execute(
        "select distinct least(e.src, e.dst) a, greatest(e.src, e.dst) b from edge e"
        " join object x on x.id = e.src join object y on y.id = e.dst"
        " where e.rel in ('related', 'member_of') and e.src <> e.dst").fetchall()


def overview(conn: psycopg.Connection, *, dsn: str | None = None) -> dict:
    """The work space page: areas with their binders, loose binders, watchers and what needs the owner."""
    binders = {b["id"]: b for b in _binders(conn)}
    mail = binder_mail(conn, dsn=dsn)
    open_work = {r["home_id"]: r for r in conn.execute(
        "select home_id, count(*) filter (where status <> 'done') as open,"
        " count(*) filter (where status = 'blocked') as blocked,"
        " count(*) filter (where status <> 'done' and due < current_date) as overdue"
        " from work_item where home_id is not null group by 1").fetchall()}
    linked: dict[int, set[int]] = {i: set() for i in binders}
    for r in _links(conn):
        if r["a"] in binders and r["b"] in binders:
            linked[r["a"]].add(r["b"])
            linked[r["b"]].add(r["a"])

    def card(i: int) -> dict:
        b = binders[i]
        m = mail["binders"].get(str(i)) or {}
        w = open_work.get(i) or {}
        return {"id": i, "kind": b["kind"], "name": b["name"], "lifecycle": (b["attrs"] or {}).get("lifecycle"),
                "mentions": m.get("mentions"), "recent": m.get("recent"), "last": m.get("last"),
                "open": w.get("open", 0), "blocked": w.get("blocked", 0), "overdue": w.get("overdue", 0)}

    areas, placed = [], set()
    for i, b in binders.items():
        if b["kind"] != "area":
            continue
        kids = sorted((k for k in linked[i] if binders[k]["kind"] != "area"),
                      key=lambda k: (binders[k]["kind"] != "project", binders[k]["kind"], binders[k]["name"].lower()))
        placed.update(kids)
        cards = [card(k) for k in kids]
        own = open_work.get(i) or {}
        areas.append({**card(i), "binders": cards,
                      "open": own.get("open", 0) + sum(c["open"] for c in cards),
                      "blocked": own.get("blocked", 0) + sum(c["blocked"] for c in cards),
                      "overdue": own.get("overdue", 0) + sum(c["overdue"] for c in cards),
                      "recent": sum(c["recent"] or 0 for c in cards)})
    areas.sort(key=lambda a: (-(a["blocked"] + a["overdue"]), -a["open"], a["name"].lower()))
    loose = [card(i) for i, b in binders.items() if b["kind"] != "area" and i not in placed]
    loose.sort(key=lambda c: (c["kind"], c["name"].lower()))
    return {"areas": areas, "loose": loose, "watchers": watchers(conn),
            "attention": attention(conn), "stale": mail["stale"], "refreshing": mail["refreshing"]}


def attention(conn: psycopg.Connection) -> list[dict]:
    """What needs the owner, strongest first: work that is overdue or blocked, and watchers with news."""
    out = []
    for w in work.attention(conn, limit=40):
        if {"overdue", "blocked", "focus"} & set(w["reasons"]):
            out.append({"kind": "work", "id": w["id"], "text": w["title"], "why": ", ".join(w["reason_text"]),
                        "home": w["home_name"], "home_id": w["home_id"]})
    for w in watchers(conn):
        if w["fresh"] and not w["paused"]:
            out.append({"kind": "watcher", "id": w["id"], "text": f"{w['name']}: {w['fresh']} new since you looked",
                        "why": "watcher", "home": w["object_name"], "home_id": w["object_id"]})
    return out[:12]


def neighbours(conn: psycopg.Connection, object_id: int, *, dsn: str | None = None) -> list[dict]:
    """Binders connected to this one: linked (the vault's related, nesting) or sharing threads."""
    names = {b["id"]: b for b in _binders(conn)}
    out: dict[int, dict] = {}
    for r in _links(conn):
        if object_id in (r["a"], r["b"]):
            other = r["b"] if r["a"] == object_id else r["a"]
            if other in names:
                out[other] = {"id": other, "kind": names[other]["kind"], "name": names[other]["name"], "why": "linked"}
    mail = binder_mail(conn, dsn=dsn)
    for n in (mail["binders"].get(str(object_id)) or {}).get("near", []):
        if n["id"] in names:
            prev = out.get(n["id"])
            out[n["id"]] = {"id": n["id"], "kind": names[n["id"]]["kind"], "name": names[n["id"]]["name"],
                            "why": ("linked · " if prev else "") + f"{n['shared']} shared threads", "shared": n["shared"]}
    return sorted(out.values(), key=lambda x: (-x.get("shared", 0), x["name"].lower()))


def found(conn: psycopg.Connection, object_id: int, *, limit: int = 8) -> dict:
    """Recent threads that mention the binder's terms and are not in it yet: people's first."""
    obj = objects.get(conn, object_id)
    if not obj:
        raise SpaceError("no such binder")
    t = terms(obj)
    members = set(objects.message_ids(conn, object_id))
    excluded = {r["src"] for r in conn.execute(
        "select src from edge where rel = 'excluded_from' and dst = %s", (object_id,)).fetchall()}
    sql, p = _term_sql(t, "m.id, m.thread_id, m.received_at")
    # Threads the owner took part in first, then people's, each newest first.
    rows = conn.execute(
        f"select x.thread_id, max(x.received_at) as last, count(*) as n, array_agg(x.id) as ids,"
        f" bool_or(not m.is_automated) as people, exists (select 1 from message y where y.thread_id = x.thread_id"
        f" and y.direction in ('out', 'self')) as mine"
        f" from ({sql}) x join message m on m.id = x.id where x.thread_id is not null"
        f" group by x.thread_id order by mine desc, people desc, last desc limit 200", p).fetchall()
    total = conn.execute(f"select count(distinct thread_id) as n from ({sql}) x", p).fetchone()["n"]
    picks = []
    for r in rows:
        if r["thread_id"] in excluded or members & set(r["ids"]):
            continue
        head = conn.execute(
            "select m.id, m.subject, m.from_name, m.from_address, m.account_id, m.medium, m.is_automated"
            " from message m where m.thread_id = %s order by m.received_at desc nulls last limit 1",
            (r["thread_id"],)).fetchone()
        picks.append({"thread_id": r["thread_id"], "message_id": head["id"], "subject": head["subject"],
                      "from": head["from_name"] or head["from_address"], "account_id": head["account_id"],
                      "medium": head["medium"], "people": r["people"], "mine": r["mine"], "n": r["n"], "last": r["last"]})
        if len(picks) >= limit:
            break
    return {"terms": t, "threads": total, "rows": picks,
            "query": urlencode([("q", " OR ".join(f'"{x}"' if " " in x else x for x in t))])}


# ---------------------------------------------------------------- watchers

def _count(conn: psycopg.Connection, query: str, since) -> dict:
    sql, p = search.messages_sql(**search.filters_from_query(query),
                                 select="count(*) as total, count(*) filter (where m.received_at > %(since)s) as fresh")
    p["since"] = since
    return conn.execute(sql, p).fetchone()


def watchers(conn: psycopg.Connection, *, object_id: int | None = None) -> list[dict]:
    rows = conn.execute(
        "select w.*, o.name as object_name, o.kind as object_kind from watcher w left join object o on o.id = w.object_id"
        + (" where w.object_id = %s" if object_id else "") + " order by w.paused, w.created_at",
        (object_id,) if object_id else ()).fetchall()
    for r in rows:
        c = _count(conn, r["query"], r["seen_at"])
        r["total"], r["fresh"] = c["total"], c["fresh"]
    return rows


def _check_query(query: str) -> str:
    query = (query or "").strip().lstrip("?")
    try:
        f = search.filters_from_query(query)
    except ValueError as exc:
        raise SpaceError(f"not a Messages selection: {exc}")
    if not any(v for v in f.values()):
        raise SpaceError("a watcher needs something to look for: a search or a filter")
    return query


def add_watcher(conn: psycopg.Connection, name: str, query: str, *, object_id: int | None = None,
                made_by: str = personal.OWNER_ID) -> int:
    name = (name or "").strip()
    if not name:
        raise SpaceError("a watcher needs a name")
    if object_id and not objects.get(conn, object_id):
        raise SpaceError("no such binder")
    return conn.execute("insert into watcher (name, query, object_id, made_by) values (%s, %s, %s, %s) returning id",
                        (name[:200], _check_query(query), object_id, made_by)).fetchone()["id"]


def _watcher(conn: psycopg.Connection, watcher_id: int) -> dict:
    w = conn.execute("select * from watcher where id = %s", (watcher_id,)).fetchone()
    if not w:
        raise SpaceError("no such watcher")
    return w


def update_watcher(conn: psycopg.Connection, watcher_id: int, *, seen: bool = False, paused: bool | None = None,
                   name: str | None = None, object_id: int | None | bool = False) -> None:
    _watcher(conn, watcher_id)
    if seen:
        conn.execute("update watcher set seen_at = now() where id = %s", (watcher_id,))
    if paused is not None:
        conn.execute("update watcher set paused = %s where id = %s", (paused, watcher_id))
    if name is not None:
        if not name.strip():
            raise SpaceError("a watcher needs a name")
        conn.execute("update watcher set name = %s where id = %s", (name.strip()[:200], watcher_id))
    if object_id is not False:
        if object_id and not objects.get(conn, object_id):
            raise SpaceError("no such binder")
        conn.execute("update watcher set object_id = %s where id = %s", (object_id or None, watcher_id))


def remove_watcher(conn: psycopg.Connection, watcher_id: int) -> None:
    _watcher(conn, watcher_id)
    conn.execute("delete from watcher where id = %s", (watcher_id,))


def take_in(conn: psycopg.Connection, watcher_id: int, *, limit: int = 500) -> int:
    """Add the watcher's new mail (since the owner last looked) to its binder, then mark it seen."""
    w = _watcher(conn, watcher_id)
    if not w["object_id"]:
        raise SpaceError("this watcher belongs to no binder")
    sql, p = search.messages_sql(**search.filters_from_query(w["query"]), select="m.id")
    p["since"] = w["seen_at"]
    ids = [r["id"] for r in conn.execute(f"select id from ({sql}) x where x.id in"
                                         f" (select id from message where received_at > %(since)s) limit {int(limit)}", p)]
    n = objects.add(conn, w["object_id"], ids) if ids else 0
    conn.execute("update watcher set seen_at = now() where id = %s", (watcher_id,))
    return n
