"""L2 meta-objects: projects, cases, collections, areas and saved searches.

An object groups other entities (messages, threads, attachments, people, orgs,
events and other objects). Its members come from three places, and every member
says which:

1. **Rules.** A rule whose action is ``{"object": id}`` writes ``member_of`` edges
   with source ``rule:<id>@<version>``. They are recomputed by ``rules.run_all``.
2. **Manual.** A human adds (``member_of``, source ``human``) or excludes
   (``excluded_from``, source ``human``). An exclusion beats every other source, and
   the latest human decision about an entity wins: adding clears an exclusion,
   excluding clears a manual addition.
3. **A stored query.** ``object.query`` is a rule-style condition list evaluated
   live against messages, so new mail joins as soon as it is ingested.

Objects can contain objects. ``members(recursive=True)`` also returns what the
nested objects hold, each with the child it came through, minus what is excluded
from either the child or the object asked about. Nesting never loops: adding an
object to one of its own descendants is refused.

Everything here writes to Talos's own tables only. Nothing touches a mail server.
"""

from __future__ import annotations

import re

import psycopg
from psycopg.types.json import Jsonb

from talos import rules

KINDS = ("project", "personal_project", "case", "collection", "area", "topic", "system", "saved_search")
MEMBER_KINDS = ("message", "thread", "attachment", "person", "org", "event", "object", "work_item", "note")
# member_of edges that count as membership. Model-made edges would be proposals, so
# they do not count until a human or a rule makes them.
COUNTED = "(e.source = 'human' or e.source like 'rule:%%')"
PARTICIPANT_RELS = ("from", "to", "cc")


class ObjectError(ValueError):
    pass


# ---------------------------------------------------------------- the object itself

def _validate_query(query: list[dict] | None) -> list[dict] | None:
    if not query:
        return None
    if not isinstance(query, list):
        raise ObjectError("a stored query is a list of conditions")
    try:
        rules.compile_conditions(query)
    except rules.RuleError as exc:
        raise ObjectError(f"bad stored query: {exc}") from exc
    return query


def create(conn: psycopg.Connection, kind: str, name: str, *, description: str | None = None,
           query: list[dict] | None = None, attrs: dict | None = None) -> int:
    """Create an object and return its entity id."""
    if kind not in KINDS:
        raise ObjectError(f"kind must be one of {', '.join(KINDS)}")
    name = (name or "").strip()
    if not name:
        raise ObjectError("an object needs a name")
    query = _validate_query(query)
    oid = conn.execute("insert into entity (kind) values ('object') returning id").fetchone()["id"]
    conn.execute(
        "insert into object (id, kind, name, description, attrs, query) values (%s, %s, %s, %s, %s, %s)",
        (oid, kind, name, description or None, Jsonb(attrs or {}), Jsonb(query) if query else None))
    return oid


def get(conn: psycopg.Connection, object_id: int) -> dict | None:
    return conn.execute("select * from object where id = %s", (object_id,)).fetchone()


def _require(conn: psycopg.Connection, object_id: int) -> dict:
    obj = get(conn, object_id)
    if not obj:
        raise ObjectError(f"no object {object_id}")
    return obj


def _touch(conn: psycopg.Connection, object_id: int) -> None:
    conn.execute("update object set updated_at = now() where id = %s", (object_id,))


def rename(conn: psycopg.Connection, object_id: int, name: str) -> None:
    _require(conn, object_id)
    name = (name or "").strip()
    if not name:
        raise ObjectError("an object needs a name")
    conn.execute("update object set name = %s, updated_at = now() where id = %s", (name, object_id))


def describe(conn: psycopg.Connection, object_id: int, description: str | None) -> None:
    _require(conn, object_id)
    conn.execute("update object set description = %s, updated_at = now() where id = %s",
                 ((description or "").strip() or None, object_id))


def set_dates(conn: psycopg.Connection, object_id: int, *, starts_on=None, ends_on=None) -> None:
    """A binder's own start and end on the timeline; None clears one (it then follows the items)."""
    from datetime import date as _date
    obj = _require(conn, object_id)
    vals = {}
    for key, v in (("starts_on", starts_on), ("ends_on", ends_on)):
        if v in (None, ""):
            vals[key] = None
        elif isinstance(v, _date):
            vals[key] = v
        else:
            try:
                vals[key] = _date.fromisoformat(str(v)[:10])
            except ValueError:
                raise ObjectError(f"{key} must be a date (YYYY-MM-DD), not {v!r}") from None
    if vals["starts_on"] and vals["ends_on"] and vals["starts_on"] > vals["ends_on"]:
        raise ObjectError("the start comes after the end")
    if (obj["starts_on"], obj["ends_on"]) != (vals["starts_on"], vals["ends_on"]):
        conn.execute("update object set starts_on = %s, ends_on = %s, updated_at = now() where id = %s",
                     (vals["starts_on"], vals["ends_on"], object_id))


_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def purpose(body: str) -> str | None:
    """The text under a body's "Purpose" heading, as the vault import reads it."""
    got, inside = [], False
    for line in (body or "").splitlines():
        if m := _HEADING.match(line):
            if inside:
                break
            inside = m.group(2).strip().lower() == "purpose"
            continue
        if inside:
            got.append(line)
    return "\n".join(got).strip() or None


def set_body(conn: psycopg.Connection, object_id: int, body: str | None) -> None:
    """Replace a binder's own text (Markdown, kept as text).

    The vault import takes the description from the body's Purpose section. While the
    description still is that section, it follows an edit of the Purpose; a description
    written by hand is left alone."""
    obj = _require(conn, object_id)
    if body is not None and not isinstance(body, str):
        raise ObjectError("the body is text (Markdown)")
    body = (body or "").replace("\r\n", "\n").rstrip()
    body = body + "\n" if body else ""
    description = obj["description"]
    if description and description == purpose(obj["body"]):
        description = purpose(body) or description
    conn.execute("update object set body = %s, description = %s, updated_at = now() where id = %s",
                 (body, description, object_id))


def archive(conn: psycopg.Connection, object_id: int, archived: bool = True) -> None:
    """Hide an object from the list. Its members and edges are kept."""
    _require(conn, object_id)
    conn.execute("update object set archived = %s, updated_at = now() where id = %s", (archived, object_id))


def set_query(conn: psycopg.Connection, object_id: int, query: list[dict] | None) -> None:
    _require(conn, object_id)
    query = _validate_query(query)
    conn.execute("update object set query = %s, updated_at = now() where id = %s",
                 (Jsonb(query) if query else None, object_id))


def promote_org(conn: psycopg.Connection, org_id: int, *, kind: str = "project") -> int:
    """Make an organisation a project: all mail from its domain, live. Returns the object id.

    The org itself becomes a manual member, so the project shows up on the org. Promoting
    the same org twice returns the project made the first time, unless it was archived.
    """
    org = conn.execute("select id, domain, name from org where id = %s", (org_id,)).fetchone()
    if not org:
        raise ObjectError(f"no org {org_id}")
    existing = conn.execute(
        "select id from object where (attrs ->> 'promoted_from_org')::bigint = %s and not archived"
        " order by id limit 1", (org_id,)).fetchone()
    if existing:
        return existing["id"]
    oid = create(conn, kind, org["name"] or org["domain"],
                 description=f"Everything from {org['domain']}",
                 query=[{"field": "from_domain", "op": "is", "value": org["domain"]}],
                 attrs={"promoted_from_org": org_id})
    add(conn, oid, [org_id])
    return oid


# ---------------------------------------------------------------- manual membership

def _entities(conn: psycopg.Connection, entity_ids: list[int]) -> dict[int, str]:
    ids = sorted({int(i) for i in entity_ids})
    if not ids:
        raise ObjectError("no entity ids given")
    found = {r["id"]: r["kind"] for r in conn.execute("select id, kind from entity where id = any(%s)", (ids,))}
    missing = [i for i in ids if i not in found]
    if missing:
        raise ObjectError(f"unknown entity ids: {', '.join(map(str, missing[:10]))}")
    return found


def _descendants(conn: psycopg.Connection, object_id: int) -> set[int]:
    """Every object nested under this one, however deep, ignoring exclusions."""
    rows = conn.execute(
        f"""
        with recursive d(id) as (
            select %s::bigint
            union
            select e.src from d join edge e on e.dst = d.id and e.rel = 'member_of' and {COUNTED}
            join object o on o.id = e.src
        )
        select id from d
        """, (object_id,)).fetchall()
    return {r["id"] for r in rows}


def add(conn: psycopg.Connection, object_id: int, entity_ids: list[int]) -> int:
    """Add entities by hand. Clears an earlier exclusion of the same entities. Returns how many were added."""
    _require(conn, object_id)
    found = _entities(conn, entity_ids)
    for eid, kind in found.items():
        if kind == "object" and object_id in _descendants(conn, eid):
            raise ObjectError(f"object {eid} contains object {object_id}; nesting it there would loop")
    ids = list(found)
    conn.execute("delete from edge where src = any(%s) and rel = 'excluded_from' and dst = %s",
                 (ids, object_id))
    cur = conn.execute(
        "insert into edge (src, rel, dst, source) select unnest(%s::bigint[]), 'member_of', %s, 'human'"
        " on conflict do nothing", (ids, object_id))
    _touch(conn, object_id)
    return cur.rowcount


def remove(conn: psycopg.Connection, object_id: int, entity_ids: list[int]) -> int:
    """Undo a manual addition. A rule or the stored query may still include the entity; exclude it to stop that."""
    _require(conn, object_id)
    cur = conn.execute(
        "delete from edge where src = any(%s) and rel = 'member_of' and dst = %s and source = 'human'",
        ([int(i) for i in entity_ids], object_id))
    _touch(conn, object_id)
    return cur.rowcount


def exclude(conn: psycopg.Connection, object_id: int, entity_ids: list[int]) -> int:
    """Keep entities out, whatever a rule or the stored query says. Clears a manual addition."""
    _require(conn, object_id)
    ids = list(_entities(conn, entity_ids))
    conn.execute("delete from edge where src = any(%s) and rel = 'member_of' and dst = %s and source = 'human'",
                 (ids, object_id))
    cur = conn.execute(
        "insert into edge (src, rel, dst, source) select unnest(%s::bigint[]), 'excluded_from', %s, 'human'"
        " on conflict do nothing", (ids, object_id))
    _touch(conn, object_id)
    return cur.rowcount


def unexclude(conn: psycopg.Connection, object_id: int, entity_ids: list[int]) -> int:
    _require(conn, object_id)
    cur = conn.execute("delete from edge where src = any(%s) and rel = 'excluded_from' and dst = %s",
                       ([int(i) for i in entity_ids], object_id))
    _touch(conn, object_id)
    return cur.rowcount


# ---------------------------------------------------------------- resolving membership

def _tree(conn: psycopg.Connection, object_id: int, recursive: bool) -> list[dict]:
    """The object and, if recursive, every nested object not excluded on the way down."""
    if not recursive:
        return conn.execute("select id, query from object where id = %s", (object_id,)).fetchall()
    return conn.execute(
        f"""
        with recursive tree(id, path) as (
            select %(root)s::bigint, array[%(root)s::bigint]
            union all
            select e.src, t.path || e.src from tree t
            join edge e on e.dst = t.id and e.rel = 'member_of' and {COUNTED}
            join object o on o.id = e.src
            where e.src <> all(t.path)
              and not exists (select 1 from edge x where x.src = e.src and x.rel = 'excluded_from'
                              and x.dst in (t.id, %(root)s))
        )
        select distinct o.id, o.query from tree t join object o on o.id = t.id
        """, {"root": object_id}).fetchall()


def _membership(conn: psycopg.Connection, object_id: int, recursive: bool) -> tuple[str, list]:
    """SQL yielding (entity_id, source, via) for every membership, exclusions already applied.

    ``via`` is the object that holds the entity directly: the object itself, or a nested one.
    """
    tree = _tree(conn, object_id, recursive)
    parts = [f"select e.src as entity_id, e.source as source, e.dst as via from edge e"
             f" where e.rel = 'member_of' and e.dst = any(%s) and {COUNTED}"]
    params: list = [[t["id"] for t in tree]]
    for t in tree:
        if t["query"]:
            where, p = rules.compile_conditions(t["query"])
            parts.append(f"select m.id, 'query', %s::bigint from message m where ({where})")
            params += [t["id"], *p]
    sql = ("select r.entity_id, r.source, r.via from (" + " union all ".join(parts) + ") r"
           " where r.entity_id <> %s and not exists (select 1 from edge x where x.src = r.entity_id"
           " and x.rel = 'excluded_from' and x.dst in (r.via, %s))")
    return sql, [*params, object_id, object_id]


LABEL = """coalesce(case en.kind
    when 'message' then coalesce(nullif(m.subject, ''), '(no subject)')
    when 'thread' then coalesce(nullif(th.subject, ''), '(no subject)')
    when 'attachment' then coalesce(att.filename, '(unnamed)')
    when 'person' then coalesce(p.display_name, p.primary_address)
    when 'org' then coalesce(o.name, o.domain)
    when 'object' then ob.name
    when 'event' then concat_ws(' ', ev.kind, ev.status, ev.system) end, '#' || en.id)"""
DETAIL = """case en.kind
    when 'message' then coalesce(m.from_name, m.from_address)
    when 'thread' then th.message_count || ' messages'
    when 'attachment' then att.content_type
    when 'person' then p.primary_address
    when 'org' then o.domain
    when 'object' then ob.kind
    when 'event' then ev.extractor end"""
ENTITY_JOINS = """
    left join message m on m.id = en.id
    left join thread th on th.id = en.id
    left join attachment att on att.id = en.id
    left join message am on am.id = att.message_id
    left join person p on p.id = en.id
    left join org o on o.id = en.id
    left join object ob on ob.id = en.id
    left join event ev on ev.id = en.id"""


def members(conn: psycopg.Connection, object_id: int, kind: str | None = None, *, limit: int = 100,
            offset: int = 0, recursive: bool = False) -> dict:
    """The object's members: rule, manual and query membership, minus exclusions.

    Returns ``{"total", "counts": {kind: n}, "rows": [...]}``. Each row has the entity id,
    its kind, a display label and detail, a date, ``sources`` (``human``, ``rule:<id>@<v>``,
    ``query``) and ``via`` / ``via_names``: the nested objects it came through (empty when direct).
    ``total`` and ``rows`` follow the kind filter; ``counts`` does not.
    """
    _require(conn, object_id)
    if kind and kind not in MEMBER_KINDS:
        raise ObjectError(f"kind must be one of {', '.join(MEMBER_KINDS)}")
    msql, params = _membership(conn, object_id, recursive)
    counts = {r["kind"]: r["n"] for r in conn.execute(
        f"with mem as ({msql}) select en.kind, count(*) n from (select distinct entity_id from mem) d"
        f" join entity en on en.id = d.entity_id group by 1", params)}
    rows = conn.execute(
        f"""
        with mem as ({msql}),
        agg as (
            select entity_id, array_agg(distinct source order by source) sources,
                   coalesce(array_agg(distinct via) filter (where via <> %s), '{{}}') via
            from mem group by entity_id
        )
        select en.id entity_id, en.kind, {LABEL} as label, {DETAIL} as detail,
               coalesce(m.received_at, th.last_at, am.received_at, ev.occurred_at) happened_at,
               coalesce(att.message_id, ev.message_id) message_id,
               a.sources, a.via,
               (select coalesce(array_agg(x.name order by x.name), '{{}}') from object x where x.id = any(a.via)) via_names
        from agg a join entity en on en.id = a.entity_id {ENTITY_JOINS}
        where (%s::text is null or en.kind = %s)
        order by happened_at desc nulls last, en.id desc
        limit %s offset %s
        """, [*params, object_id, kind, kind, min(int(limit), 500), max(int(offset), 0)]).fetchall()
    total = counts.get(kind, 0) if kind else sum(counts.values())
    return {"total": total, "counts": counts, "rows": rows}


def excluded(conn: psycopg.Connection, object_id: int, *, limit: int = 200) -> list[dict]:
    """What has been kept out of the object by hand, newest first."""
    return conn.execute(
        f"select en.id entity_id, en.kind, {LABEL} as label, {DETAIL} as detail, x.created_at excluded_at"
        f" from edge x join entity en on en.id = x.src {ENTITY_JOINS}"
        f" where x.rel = 'excluded_from' and x.dst = %s order by x.created_at desc limit %s",
        (object_id, limit)).fetchall()


def list_objects(conn: psycopg.Connection, *, include_archived: bool = False) -> list[dict]:
    """Every object with its direct member counts per kind."""
    objs = conn.execute(
        "select id, kind, name, description, query, attrs, archived, created_at, updated_at from object"
        + ("" if include_archived else " where not archived") + " order by archived, lower(name), id").fetchall()
    for o in objs:
        msql, params = _membership(conn, o["id"], False)
        o["counts"] = {r["kind"]: r["n"] for r in conn.execute(
            f"with mem as ({msql}) select en.kind, count(*) n from (select distinct entity_id from mem) d"
            f" join entity en on en.id = d.entity_id group by 1", params)}
        o["members"] = sum(o["counts"].values())
    return objs


def objects_of(conn: psycopg.Connection, entity_id: int) -> list[dict]:
    """The objects an entity belongs to, directly or (for a message) through its thread.

    Each row: id, kind, name, archived, ``sources`` for the entity itself and
    ``thread_sources`` for its thread. Exclusions of the entity are respected.
    """
    ent = conn.execute("select id, kind from entity where id = %s", (entity_id,)).fetchone()
    if not ent:
        return []
    thread_id = None
    if ent["kind"] == "message":
        thread_id = conn.execute("select thread_id from message where id = %s", (entity_id,)).fetchone()["thread_id"]
    subjects = [entity_id] + ([thread_id] if thread_id else [])
    found: dict[int, dict] = {}

    def note(object_id: int, source: str, through_thread: bool) -> None:
        row = found.setdefault(object_id, {"sources": set(), "thread_sources": set()})
        row["thread_sources" if through_thread else "sources"].add(source)

    for r in conn.execute(
            f"select e.src, e.dst, e.source from edge e where e.src = any(%s) and e.rel = 'member_of' and {COUNTED}"
            f" and not exists (select 1 from edge x where x.src = e.src and x.rel = 'excluded_from' and x.dst = e.dst)",
            (subjects,)):
        note(r["dst"], r["source"], r["src"] != entity_id)
    if ent["kind"] == "message":
        for o in conn.execute("select id, query from object where query is not null").fetchall():
            where, p = rules.compile_conditions(o["query"])
            if conn.execute(f"select 1 from message m where m.id = %s and ({where})", [entity_id, *p]).fetchone():
                note(o["id"], "query", False)
    if not found:
        return []
    excluded_from = {r["dst"] for r in conn.execute(
        "select dst from edge where src = %s and rel = 'excluded_from'", (entity_id,))}
    out = []
    for o in conn.execute("select id, kind, name, archived from object where id = any(%s) order by lower(name)",
                          (list(found),)):
        if o["id"] in excluded_from:
            continue
        o["sources"] = sorted(found[o["id"]]["sources"])
        o["thread_sources"] = sorted(found[o["id"]]["thread_sources"])
        out.append(o)
    return out


# ---------------------------------------------------------------- common denominators

def message_ids(conn: psycopg.Connection, object_id: int, *, recursive: bool = False) -> list[int]:
    """The messages an object covers: its message members plus every message of its thread members."""
    msql, params = _membership(conn, object_id, recursive)
    return [r["id"] for r in conn.execute(
        f"""
        with mem as ({msql}), d as (select distinct entity_id from mem)
        select id from (
            select m.id from message m join d on d.entity_id = m.id
            union
            select m.id from message m join d on d.entity_id = m.thread_id
        ) u
        where not exists (select 1 from edge x where x.src = u.id and x.rel = 'excluded_from' and x.dst = %s)
        order by id
        """, [*params, object_id])]


def common(conn: psycopg.Connection, object_id: int, *, recursive: bool = False, top: int = 10) -> dict:
    """What an object's mail has in common: people, orgs, labels, folders, attachment types, dates.

    Computed over the messages of its message and thread members. People are counted from
    the from/to/cc edges (the owner's own addresses left out, since they are on everything),
    orgs through ``person.org_id``. Each count says in how many messages and threads it
    appears, so "the org behind all 40 threads" is the first org row.
    """
    _require(conn, object_id)
    ids = message_ids(conn, object_id, recursive=recursive)
    span = conn.execute(
        "select count(*) messages, count(distinct thread_id) threads, min(received_at) first_at,"
        " max(received_at) last_at from message where id = any(%s)", (ids,)).fetchone()
    if not ids:
        return {**span, "people": [], "orgs": [], "labels": [], "folders": [], "attachment_types": []}
    rels = list(PARTICIPANT_RELS)
    people = conn.execute(
        "select p.id, coalesce(p.display_name, p.primary_address) as name, p.primary_address as address,"
        " p.org_id, o.domain as org_domain, count(distinct e.src) messages, count(distinct m.thread_id) threads,"
        " array_agg(distinct e.rel) roles"
        " from edge e join person p on p.id = e.dst join message m on m.id = e.src left join org o on o.id = p.org_id"
        " where e.src = any(%s) and e.rel = any(%s) and not p.is_me"
        " group by p.id, o.domain order by threads desc, messages desc, p.id limit %s", (ids, rels, top)).fetchall()
    orgs = conn.execute(
        "select o.id, o.domain, coalesce(o.name, o.domain) as name, count(distinct e.src) messages,"
        " count(distinct m.thread_id) threads, count(distinct p.id) people"
        " from edge e join person p on p.id = e.dst join org o on o.id = p.org_id join message m on m.id = e.src"
        " where e.src = any(%s) and e.rel = any(%s) and not p.is_me"
        " group by o.id order by threads desc, messages desc, o.id limit %s", (ids, rels, top)).fetchall()
    labels = conn.execute(
        "select x as label, count(distinct l.message_id) messages from message_location l, unnest(l.labels) x"
        " where l.message_id = any(%s) and l.present group by 1 order by 2 desc, 1 limit %s", (ids, top)).fetchall()
    folders = conn.execute(
        "select coalesce(l.folder_path, l.folder) as folder, l.account_id, count(distinct l.message_id) messages from message_location l"
        " where l.message_id = any(%s) and l.present group by 1, 2 order by 3 desc, 1 limit %s", (ids, top)).fetchall()
    types = conn.execute(
        "select content_type, count(*) n, sum(size_bytes) bytes from attachment where message_id = any(%s)"
        " group by 1 order by 2 desc, 1 limit %s", (ids, top)).fetchall()
    return {**span, "people": people, "orgs": orgs, "labels": labels, "folders": folders,
            "attachment_types": types}
