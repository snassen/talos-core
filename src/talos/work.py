"""Work items: the things to decide or do. Talos Web is where the owner acts on them.

A work item has one status (Obsidian Talos's vocabulary: inbox, next, doing, blocked,
someday, done), at most one home (a project, area, topic or system object), secondary
'related' objects, and links to the messages it is about. Every change is written to
work_item_event with who made it, so the board's history can be read and undone.

This module is the one place that writes work items; the web API and the vault importer
both go through it.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from talos import personal

STATUSES = ("inbox", "next", "doing", "blocked", "someday", "done")
EDITABLE = ("title", "status", "home_id", "focus", "start_on", "due", "review_after", "body", "position")


class WorkError(ValueError):
    pass


def _check(fields: dict) -> None:
    if "status" in fields and fields["status"] not in STATUSES:
        raise WorkError(f"unknown status {fields['status']!r}; use one of {', '.join(STATUSES)}")
    if "title" in fields and not str(fields["title"] or "").strip():
        raise WorkError("a work item needs a title")
    for key in ("start_on", "due", "review_after"):
        v = fields.get(key)
        if v not in (None, "") and not isinstance(v, date):
            try:
                fields[key] = date.fromisoformat(str(v)[:10])
            except ValueError:
                raise WorkError(f"{key} must be a date (YYYY-MM-DD), not {v!r}") from None
        elif v == "":
            fields[key] = None
    if fields.get("start_on") and fields.get("due") and fields["start_on"] > fields["due"]:
        raise WorkError("the start comes after the due date")


def _home_ok(conn: psycopg.Connection, home_id: int | None) -> None:
    if home_id is not None and not conn.execute("select 1 from object where id = %s", (home_id,)).fetchone():
        raise WorkError(f"no object {home_id} to be the home")


def _messages_ok(conn: psycopg.Connection, message_ids: list[int]) -> None:
    """Refuse ids that are not messages, before anything is written."""
    ids = sorted({int(i) for i in message_ids})
    if not ids:
        return
    found = {r["id"] for r in conn.execute("select id from message where id = any(%s)", (ids,))}
    missing = [i for i in ids if i not in found]
    if missing:
        raise WorkError(f"no message {', '.join(map(str, missing[:10]))}")


def create(conn: psycopg.Connection, title: str, *, by: str = personal.OWNER_ID, status: str = "inbox",
           home_id: int | None = None, focus: bool = False, start_on=None, due=None, review_after=None, body: str = "",
           source: dict | None = None, origin: dict | None = None, message_ids: list[int] | None = None,
           created_at=None) -> int:
    fields = {"title": title, "status": status, "start_on": start_on, "due": due, "review_after": review_after}
    _check(fields)
    _home_ok(conn, home_id)
    _messages_ok(conn, message_ids or [])
    with conn.transaction():
        wid = conn.execute("insert into entity (kind) values ('work_item') returning id").fetchone()["id"]
        # The end of its column. Positions are per status across every home, so the Work board
        # (all homes) and a binder's board (one home) both keep the order a move gave.
        position = end_position(conn, status)
        conn.execute(
            "insert into work_item (id, title, status, home_id, focus, start_on, due, review_after, body, source, origin,"
            " position, created_at, done_at) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
            " coalesce(%s, now()), case when %s = 'done' then coalesce(%s, now()) end)",
            (wid, fields["title"].strip(), status, home_id, focus, fields["start_on"], fields["due"], fields["review_after"], body,
             Jsonb(source or {}), Jsonb(origin or {}), position, created_at, status, created_at))
        conn.execute("insert into work_item_event (work_item_id, field, new_value, by) values (%s, 'created', %s, %s)",
                     (wid, Jsonb({"title": fields["title"], "status": status, "home_id": home_id}), by))
        for mid in message_ids or []:
            link_message(conn, wid, mid, by=by)
    return wid


def update(conn: psycopg.Connection, work_item_id: int, *, by: str = personal.OWNER_ID, **changes: Any) -> dict:
    """Change some fields; returns the new row. Unchanged fields write no event."""
    unknown = set(changes) - set(EDITABLE)
    if unknown:
        raise WorkError(f"cannot change {', '.join(sorted(unknown))}")
    _check(changes)
    if "home_id" in changes:
        _home_ok(conn, changes["home_id"])
    with conn.transaction():
        row = conn.execute("select * from work_item where id = %s for update", (work_item_id,)).fetchone()
        if not row:
            raise WorkError(f"no work item {work_item_id}")
        diff = {k: v for k, v in changes.items() if row[k] != v}
        start, due = diff.get("start_on", row["start_on"]), diff.get("due", row["due"])
        if start and due and start > due:
            raise WorkError("the start comes after the due date")
        if not diff:
            return row
        sets = ", ".join(f"{k} = %({k})s" for k in diff)
        extra = ""
        if "status" in diff:
            extra = ", done_at = case when %(status)s = 'done' then now() else null end"
        conn.execute(f"update work_item set {sets}{extra}, updated_at = now() where id = %(id)s",
                     {**diff, "id": work_item_id})
        for k, v in diff.items():
            conn.execute(
                "insert into work_item_event (work_item_id, field, old_value, new_value, by) values (%s, %s, %s, %s, %s)",
                (work_item_id, k, Jsonb(_json(row[k])), Jsonb(_json(v)), by))
        return conn.execute("select * from work_item where id = %s", (work_item_id,)).fetchone()


def _json(v):
    return v.isoformat() if isinstance(v, date) else v


def link_message(conn: psycopg.Connection, work_item_id: int, message_id: int, *, by: str = personal.OWNER_ID) -> None:
    conn.execute("insert into edge (src, rel, dst, source) values (%s, 'about', %s, %s) on conflict do nothing",
                 (work_item_id, message_id, "human" if by == personal.OWNER_ID else by))


def unlink_message(conn: psycopg.Connection, work_item_id: int, message_id: int) -> None:
    conn.execute("delete from edge where src = %s and rel = 'about' and dst = %s", (work_item_id, message_id))


def relate(conn: psycopg.Connection, work_item_id: int, object_id: int, *, by: str = personal.OWNER_ID) -> None:
    conn.execute("insert into edge (src, rel, dst, source) values (%s, 'related', %s, %s) on conflict do nothing",
                 (work_item_id, object_id, "human" if by == personal.OWNER_ID else by))


def get(conn: psycopg.Connection, work_item_id: int) -> dict | None:
    row = conn.execute(
        "select w.*, o.name as home_name, o.kind as home_kind from work_item w"
        " left join object o on o.id = w.home_id where w.id = %s", (work_item_id,)).fetchone()
    if not row:
        return None
    row["messages"] = conn.execute(
        "select m.id, m.account_id, m.medium, m.subject, m.from_name, m.from_address, m.received_at"
        " from edge e join message m on m.id = e.dst where e.src = %s and e.rel = 'about'"
        " order by m.received_at", (work_item_id,)).fetchall()
    row["related"] = conn.execute(
        "select o.id, o.kind, o.name from edge e join object o on o.id = e.dst"
        " where e.src = %s and e.rel = 'related' order by o.name", (work_item_id,)).fetchall()
    # Calendar entries split off from it (the timeline's "make a calendar entry").
    row["calendar"] = conn.execute(
        "select e.id, e.title, e.starts_at, e.ends_at, e.all_day, c.name as calendar, c.color from calendar_entry e"
        " join calendar c on c.id = e.calendar_id where e.work_item_id = %s and not e.gone and e.removed_at is null"
        " and not c.gone order by e.starts_at", (work_item_id,)).fetchall()
    row["history"] = conn.execute(
        "select at, field, old_value, new_value, by from work_item_event where work_item_id = %s order by at, id",
        (work_item_id,)).fetchall()
    return row


def of_message(conn: psycopg.Connection, message_id: int) -> list[dict]:
    """The work items a message is part of (for the message drawer)."""
    return conn.execute(
        "select w.id, w.title, w.status, o.id as home_id, o.name as home_name from edge e"
        " join work_item w on w.id = e.src left join object o on o.id = w.home_id"
        " where e.dst = %s and e.rel = 'about' order by w.created_at", (message_id,)).fetchall()


# ---------------------------------------------------------------- views for the web UI

# Why an item needs attention, strongest first. The vault's "Needs attention" base listed
# inbox, doing, blocked, focus and overdue; review_after that has come round is added here.
REASONS = {
    "overdue": "overdue",
    "blocked": "blocked",
    "doing": "in progress",
    "review": "review due",
    "focus": "focus",
    "inbox": "inbox",
}
_STATUS_ORDER = "array_position(array['inbox','next','doing','blocked','someday','done'], w.status)"
_LIST = """
    select w.id, w.title, w.status, w.home_id, o.name as home_name, o.kind as home_kind, w.focus, w.start_on, w.due,
           w.review_after, w.position, w.created_at, w.updated_at, w.done_at,
           (select count(distinct e.dst) from edge e where e.src = w.id and e.rel = 'about') as message_count,
           (select coalesce(json_agg(json_build_object('id', r.id, 'name', r.name, 'kind', r.kind) order by lower(r.name)), '[]')
            from (select distinct x.id, x.name, x.kind from edge e join object x on x.id = e.dst
                  where e.src = w.id and e.rel = 'related') r) as related,
           coalesce(w.status <> 'done' and w.due < %(today)s, false) as overdue,
           coalesce(w.status <> 'done' and w.review_after <= %(today)s, false) as review_due
    from work_item w left join object o on o.id = w.home_id
"""


def list_items(conn: psycopg.Connection, *, status: str | list[str] | None = None, home_id: int | str | None = None,
               focus: bool | None = None, q: str | None = None, overdue: bool | None = None,
               today: date | None = None) -> list[dict]:
    """Work items for a board or a list, in board order: status, then position within the column.

    home_id "none" lists the items without a home. Each row has its home's name and kind, its
    related binders (id, name, kind), how many messages it is linked to, and whether it is
    overdue or due for review today."""
    statuses = [status] if isinstance(status, str) else list(status or [])
    bad = [s for s in statuses if s not in STATUSES]
    if bad:
        raise WorkError(f"unknown status {bad[0]!r}; use one of {', '.join(STATUSES)}")
    where, params = [], {"today": today or date.today()}
    if statuses:
        where.append("w.status = any(%(statuses)s)")
        params["statuses"] = statuses
    if home_id == "none":
        where.append("w.home_id is null")
    elif home_id is not None:
        where.append("w.home_id = %(home)s")
        params["home"] = int(home_id)
    if focus is not None:
        where.append("w.focus = %(focus)s")
        params["focus"] = focus
    if overdue is not None:
        where.append(("" if overdue else "not ") + "coalesce(w.status <> 'done' and w.due < %(today)s, false)")
    if q and q.strip():
        where.append("(w.title ilike %(q)s or w.body ilike %(q)s)")
        params["q"] = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    sql = _LIST + (" where " + " and ".join(where) if where else "") + f" order by {_STATUS_ORDER}, w.position, w.id"
    return conn.execute(sql, params).fetchall()


def attention(conn: psycopg.Connection, *, today: date | None = None, limit: int = 100) -> list[dict]:
    """Open items that need the owner: inbox, doing or blocked, focus, overdue, or due for review.

    Each row carries ``reasons`` (codes from REASONS, strongest first) and ``reason_text``.
    Done items never need attention. The strongest reason orders the list; then the due date."""
    today = today or date.today()
    rows = conn.execute(
        _LIST + " where w.status <> 'done' and (w.status in ('inbox', 'doing', 'blocked') or w.focus"
        " or w.due < %(today)s or w.review_after <= %(today)s)", {"today": today}).fetchall()
    for r in rows:
        found = {"overdue": r["overdue"], "blocked": r["status"] == "blocked", "doing": r["status"] == "doing",
                 "review": r["review_due"], "focus": r["focus"], "inbox": r["status"] == "inbox"}
        r["reasons"] = [k for k in REASONS if found[k]]
        r["reason_text"] = [REASONS[k] for k in r["reasons"]]
    order = list(REASONS)
    rows.sort(key=lambda r: (order.index(r["reasons"][0]), r["due"] or date.max, r["position"], r["id"]))
    return rows[:limit]


def end_position(conn: psycopg.Connection, status: str) -> float:
    """A position after every item in a column, for a move that does not say where."""
    return conn.execute("select coalesce(max(position), 0) + 1 as p from work_item where status = %s",
                        (status,)).fetchone()["p"]


def change_messages(conn: psycopg.Connection, work_item_id: int, *, add: list[int] = (), remove: list[int] = (),
                    by: str = personal.OWNER_ID) -> list[dict]:
    """Link and unlink messages, recorded as one 'messages' event. Returns the linked messages."""
    if not conn.execute("select 1 from work_item where id = %s", (work_item_id,)).fetchone():
        raise WorkError(f"no work item {work_item_id}")
    add, remove = sorted({int(i) for i in add}), sorted({int(i) for i in remove})
    _messages_ok(conn, add)
    with conn.transaction():
        before = {r["dst"] for r in conn.execute(
            "select distinct dst from edge where src = %s and rel = 'about'", (work_item_id,))}
        for mid in add:
            link_message(conn, work_item_id, mid, by=by)
        for mid in remove:
            unlink_message(conn, work_item_id, mid)
        after = {r["dst"] for r in conn.execute(
            "select distinct dst from edge where src = %s and rel = 'about'", (work_item_id,))}
        if before != after:
            conn.execute(
                "insert into work_item_event (work_item_id, field, old_value, new_value, by) values (%s, 'messages', %s, %s, %s)",
                (work_item_id, Jsonb(sorted(before)), Jsonb(sorted(after)), by))
            conn.execute("update work_item set updated_at = now() where id = %s", (work_item_id,))
    return get(conn, work_item_id)["messages"]
