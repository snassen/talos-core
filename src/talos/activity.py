"""What has happened in a binder: its activity log, its facts and its notes.

The log merges four streams, newest first:

- **work**: the object's own work items (those whose home it is): each one's creation, at
  the item's ``created_at`` (the vault date for an import), and every later change from
  ``work_item_event`` (status moves, edits, linked messages). Pure reordering is left out.
- **messages**: the mail and chat linked to the object: its message members, the messages
  of its thread members, what its stored query matches, and the messages its work items
  are about. Each is shown once, at ``received_at``; exclusions are respected.
- **notes**: a note's creation and, when it was changed after that, its update.
- **object**: the object's own creation (the vault's ``created`` date for an import) and
  the import itself.

Each stream is read with its own indexed query, ordered and limited, and the streams are
merged here, so a binder on the full archive costs a handful of index scans. Paging uses a
cursor of (at, rank, id), which is unique, so items that share a timestamp (an import
writes many at once) are neither skipped nor repeated across pages.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

import psycopg

from talos import objects, rules

FILTERS = ("all", "work", "messages", "notes")
MAX_LIMIT = 200
# Rank breaks ties between streams at the same instant; together with the id it makes the
# cursor unique.
RANK = {"work_event": 1, "work_created": 2, "message": 3, "note_updated": 4, "note_created": 5,
        "object_imported": 6, "object_created": 7}
FAR_FUTURE = datetime(9999, 1, 1, tzinfo=timezone.utc)


class ActivityError(ValueError):
    pass


# ---------------------------------------------------------------- the cursor

def encode_cursor(at: datetime, rank: int, id_: int) -> str:
    return f"{at.isoformat()}~{rank}~{id_}"


def decode_cursor(cursor: str | None) -> tuple[datetime, int, int]:
    """(at, rank, id) to page after. A bare timestamp pages from before that instant."""
    if not cursor:
        return FAR_FUTURE, 0, 0
    parts = cursor.split("~")
    try:
        at = datetime.fromisoformat(parts[0].replace(" ", "+").replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if len(parts) == 1:
            return at, 0, 0
        return at, int(parts[1]), int(parts[2])
    except (ValueError, IndexError):
        raise ActivityError("before is a cursor from an earlier page, or a timestamp") from None


def _after(at: datetime, rank: int, id_: int, cur: tuple[datetime, int, int]) -> bool:
    """True when the item comes after the cursor in newest-first order."""
    return (at, rank, id_) < cur


# ---------------------------------------------------------------- linked messages

def _linked_messages(conn: psycopg.Connection, object_id: int) -> tuple[str, list]:
    """SQL yielding (message_id, work_item_id) for every message linked to the object.

    work_item_id is set when the link is through one of its work items, null otherwise."""
    obj = conn.execute("select query from object where id = %s", (object_id,)).fetchone()
    parts = [
        f"select e.src as mid, null::bigint as wid from edge e where e.dst = %s and e.rel = 'member_of'"
        f" and {objects.COUNTED}",
        f"select m.id, null::bigint from edge e join message m on m.thread_id = e.src"
        f" where e.dst = %s and e.rel = 'member_of' and {objects.COUNTED}",
        "select a.dst, w.id from work_item w join edge a on a.src = w.id and a.rel = 'about' where w.home_id = %s",
    ]
    params: list = [object_id, object_id, object_id]
    if obj and obj["query"]:
        where, p = rules.compile_conditions(obj["query"])
        parts.append(f"select m.id, null::bigint from message m where ({where})")
        params += p
    return " union all ".join(parts), params


def message_count(conn: psycopg.Connection, object_id: int) -> int:
    sql, params = _linked_messages(conn, object_id)
    return conn.execute(
        f"select count(distinct l.mid) n from ({sql}) l join message m on m.id = l.mid"
        " where not exists (select 1 from edge x where x.src = m.id and x.rel = 'excluded_from' and x.dst = %s)",
        [*params, object_id]).fetchone()["n"]


# ---------------------------------------------------------------- the streams

def _work(conn, object_id: int, cur, n: int) -> list[dict]:
    at, rank, id_ = cur
    events = conn.execute(
        """
        select ev.id, ev.at, ev.field, ev.old_value, ev.new_value, ev.by,
               w.id as work_item_id, w.title, w.status
        from work_item w join work_item_event ev on ev.work_item_id = w.id
        where w.home_id = %(oid)s and ev.field not in ('created', 'position')
          and (ev.at, %(rank)s, ev.id) < (%(at)s, %(crank)s, %(cid)s)
        order by ev.at desc, ev.id desc limit %(n)s
        """, {"oid": object_id, "rank": RANK["work_event"], "at": at, "crank": rank, "cid": id_, "n": n}).fetchall()
    homes = {v for e in events if e["field"] == "home_id" for v in (e["old_value"], e["new_value"]) if v is not None}
    names = {r["id"]: r["name"] for r in conn.execute("select id, name from object where id = any(%s)", (list(homes),))} \
        if homes else {}
    out = []
    for e in events:
        item = {"type": "work", "event": e["field"], "at": e["at"], "rank": RANK["work_event"], "id": e["id"],
                "work_item_id": e["work_item_id"], "title": e["title"], "status": e["status"],
                "old_value": e["old_value"], "new_value": e["new_value"], "by": e["by"]}
        if e["field"] == "home_id":
            item["old_name"], item["new_name"] = names.get(e["old_value"]), names.get(e["new_value"])
        out.append(item)
    created = conn.execute(
        """
        select w.id, w.created_at as at, w.title, w.status,
               (select ev.by from work_item_event ev where ev.work_item_id = w.id and ev.field = 'created'
                order by ev.id limit 1) as by,
               (select ev.new_value ->> 'status' from work_item_event ev where ev.work_item_id = w.id
                and ev.field = 'created' order by ev.id limit 1) as created_status
        from work_item w
        where w.home_id = %(oid)s and (w.created_at, %(rank)s, w.id) < (%(at)s, %(crank)s, %(cid)s)
        order by w.created_at desc, w.id desc limit %(n)s
        """, {"oid": object_id, "rank": RANK["work_created"], "at": at, "crank": rank, "cid": id_, "n": n}).fetchall()
    out += [{"type": "work", "event": "created", "at": r["at"], "rank": RANK["work_created"], "id": r["id"],
             "work_item_id": r["id"], "title": r["title"], "status": r["status"],
             "created_status": r["created_status"], "by": r["by"]} for r in created]
    return out


def _messages(conn, object_id: int, cur, n: int) -> list[dict]:
    at, rank, id_ = cur
    sql, params = _linked_messages(conn, object_id)
    rows = conn.execute(
        f"""
        with linked as ({sql})
        select m.id, m.received_at as at, m.subject, m.account_id, m.medium, m.direction, m.from_name, m.from_address,
               coalesce(array_agg(distinct w.title) filter (where w.id is not null), '{{}}') as via_work
        from linked l join message m on m.id = l.mid left join work_item w on w.id = l.wid
        where m.received_at is not null
          and (m.received_at, %s, m.id) < (%s, %s, %s)
          and not exists (select 1 from edge x where x.src = m.id and x.rel = 'excluded_from' and x.dst = %s)
        group by m.id
        order by m.received_at desc, m.id desc limit %s
        """, [*params, RANK["message"], at, rank, id_, object_id, n]).fetchall()
    return [{"type": "message", "event": "received" if r["direction"] != "out" else "sent", "at": r["at"],
             "rank": RANK["message"], "id": r["id"], "message_id": r["id"], "title": r["subject"] or "(no subject)",
             "account_id": r["account_id"], "medium": r["medium"], "direction": r["direction"],
             "from": r["from_name"] or r["from_address"], "via_work": r["via_work"]} for r in rows]


# A note counts as updated only when it changed after it was made, and, for an import,
# after the import wrote it (the import sets created_at to the vault date).
_NOTE_UPDATED = ("n.updated_at > n.created_at + interval '1 minute' and (not n.origin ? 'imported_at'"
                 " or n.updated_at > (n.origin ->> 'imported_at')::timestamptz + interval '1 minute')")


def _notes(conn, object_id: int, cur, n: int) -> list[dict]:
    at, rank, id_ = cur
    out = []
    for event, col, extra in (("created", "created_at", "true"), ("updated", "updated_at", _NOTE_UPDATED)):
        r = RANK["note_" + event]
        rows = conn.execute(
            f"select n.id, n.{col} as at, n.kind, n.title from note n where n.object_id = %s and {extra}"
            f" and (n.{col}, %s, n.id) < (%s, %s, %s) order by n.{col} desc, n.id desc limit %s",
            (object_id, r, at, rank, id_, n)).fetchall()
        out += [{"type": "note", "event": event, "at": x["at"], "rank": r, "id": x["id"], "note_id": x["id"],
                 "kind": x["kind"], "title": x["title"]} for x in rows]
    return out


def _as_instant(v) -> datetime | None:
    """A vault 'created' value (a date or a timestamp, as text) as an aware datetime."""
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        try:
            d = datetime.combine(date.fromisoformat(str(v)[:10]), time(12))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _object(obj: dict, cur) -> list[dict]:
    out = []
    origin = obj["origin"] or {}
    created = _as_instant((obj["attrs"] or {}).get("created")) or obj["created_at"]
    out.append({"type": "object", "event": "created", "at": created, "rank": RANK["object_created"], "id": obj["id"],
                "title": obj["name"], "kind": obj["kind"]})
    imported = _as_instant(origin.get("imported_at"))
    if imported:
        out.append({"type": "object", "event": "imported", "at": imported, "rank": RANK["object_imported"],
                    "id": obj["id"], "title": obj["name"], "kind": obj["kind"], "path": origin.get("path")})
    return [x for x in out if _after(x["at"], x["rank"], x["id"], cur)]


# ---------------------------------------------------------------- the log

def activity(conn: psycopg.Connection, object_id: int, *, show: str = "all", before: str | None = None,
             limit: int = 100) -> dict:
    """The object's activity, newest first: ``{"items": [...], "next": cursor or None}``.

    ``show`` is all, work, messages or notes (the object's own creation shows under all).
    Pass ``next`` back as ``before`` for the page after."""
    obj = objects.get(conn, object_id)
    if not obj:
        raise objects.ObjectError(f"no object {object_id}")
    if show not in FILTERS:
        raise ActivityError(f"show must be one of {', '.join(FILTERS)}")
    limit = max(1, min(int(limit), MAX_LIMIT))
    cur = decode_cursor(before)
    n = limit + 1
    items: list[dict] = []
    if show in ("all", "work"):
        items += _work(conn, object_id, cur, n)
    if show in ("all", "messages"):
        items += _messages(conn, object_id, cur, n)
    if show in ("all", "notes"):
        items += _notes(conn, object_id, cur, n)
    if show == "all":
        items += _object(obj, cur)
    items.sort(key=lambda x: (x["at"], x["rank"], x["id"]), reverse=True)
    more = len(items) > limit
    items = items[:limit]
    for x in items:
        x["key"] = encode_cursor(x["at"], x["rank"], x["id"])
    return {"items": items, "next": items[-1]["key"] if more and items else None}


# ---------------------------------------------------------------- facts and notes

def facts(conn: psycopg.Connection, object_id: int) -> dict:
    """The counts for a binder's About panel."""
    w = conn.execute("select count(*) filter (where status <> 'done') open, count(*) filter (where status = 'done') done"
                     " from work_item where home_id = %s", (object_id,)).fetchone()
    notes = conn.execute("select count(*) n from note where object_id = %s", (object_id,)).fetchone()["n"]
    return {"work_open": w["open"], "work_done": w["done"], "messages": message_count(conn, object_id), "notes": notes}


def notes(conn: psycopg.Connection, object_id: int, *, limit: int = 500) -> list[dict]:
    """The object's notes, newest first, with their bodies."""
    return conn.execute(
        "select id, kind, title, body, attrs, created_at, updated_at, origin ->> 'path' as path from note"
        " where object_id = %s order by created_at desc, id desc limit %s", (object_id, limit)).fetchall()
