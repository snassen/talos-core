"""The timeline: Work › Timeline, a Gantt over the binders, to see whether the owner has taken on too much
(docs/timeline.md).

A work item is a bar from its start (start_on) to its due date. With a due date alone it is a
milestone on that day; with a start alone it runs on past the window, open-ended. With neither it
is undated: it is listed under its binder so a date can be given, but it has no place on the line.
A binder's bar is its own start and end (object.starts_on, ends_on) where it has them, and
otherwise spans its items.

The calendar is shown beside it, not as Gantt bars: the hours taken per day by the visible calendars
(busy, tentative or away; free and cancelled entries do not count, and overlapping entries count
once), and the entries split off from a work item on that item's row. The load per day is those
hours, the open items running that day, and the items due.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import psycopg

from talos import objects

MAX_DAYS = 800
BUSY = ("busy", "tentative", "oof", "workingElsewhere")


class TimelineError(ValueError):
    pass


def _zone(conn: psycopg.Connection) -> ZoneInfo:
    """The database's time zone (the owner's own, e.g. Europe/Stockholm): days begin at its midnight."""
    try:
        return ZoneInfo(conn.execute("show timezone").fetchone()["TimeZone"])
    except Exception:  # noqa: BLE001 — an odd zone name falls back to UTC rather than failing the page
        return ZoneInfo("UTC")


def _span(it: dict) -> tuple[date | None, date | None]:
    """An item's first and last day on the line: (start, due), a milestone (due, due), or open (start, None)."""
    s, d = it["start_on"], it["due"]
    if s and d:
        return s, d
    if d:
        return d, d
    return s, None


def busy_hours(entries: list[dict], days: list[date], zone: ZoneInfo) -> dict[date, float]:
    """Hours per day covered by at least one entry (the union, so a double booking counts once)."""
    out = {}
    for d in days:
        d0 = datetime.combine(d, time(0), tzinfo=zone)
        d1 = datetime.combine(d + timedelta(days=1), time(0), tzinfo=zone)
        spans = sorted((max(e["starts_at"], d0), min(e["ends_at"], d1)) for e in entries
                       if e["starts_at"] < d1 and e["ends_at"] > d0)
        total, cur_s, cur_e = timedelta(0), None, None
        for s, e in spans:
            if cur_e is None or s > cur_e:
                if cur_e is not None:
                    total += cur_e - cur_s
                cur_s, cur_e = s, e
            else:
                cur_e = max(cur_e, e)
        if cur_e is not None:
            total += cur_e - cur_s
        out[d] = round(total.total_seconds() / 3600, 2)
    return out


def timeline(conn: psycopg.Connection, start: date, end: date, *, kind: str | None = None, binder: int | None = None,
             done: bool = False, focus: bool = False) -> dict:
    """The binders with their items in [start, end], the undated items, and the load per day."""
    if end < start:
        raise TimelineError("the end comes before the start")
    if (end - start).days > MAX_DAYS:
        raise TimelineError(f"ask for at most {MAX_DAYS} days at a time")
    where, params = [], {}
    if not done:
        where.append("w.status <> 'done'")
    if focus:
        where.append("w.focus")
    if binder is not None:
        if not objects.get(conn, binder):
            raise TimelineError(f"no binder {binder}")
        where.append("w.home_id = any(%(homes)s)")
        params["homes"] = sorted(objects._descendants(conn, binder))
    if kind:
        where.append("o.kind = %(kind)s")
        params["kind"] = kind
    items = conn.execute(
        "select w.id, w.title, w.status, w.focus, w.start_on, w.due, w.home_id, w.done_at, o.kind as home_kind"
        " from work_item w left join object o on o.id = w.home_id"
        + (" where " + " and ".join(where) if where else "") + " order by w.id", params).fetchall()

    # Entries split off from these items, and the calendar's busy hours, in the window.
    zone = _zone(conn)
    lo = datetime.combine(start, time(0), tzinfo=zone)
    hi = datetime.combine(end + timedelta(days=1), time(0), tzinfo=zone)
    linked: dict[int, list[dict]] = {}
    for r in conn.execute(
            "select e.id, e.work_item_id, e.title, e.starts_at, e.ends_at, e.all_day, c.color from calendar_entry e"
            " join calendar c on c.id = e.calendar_id where e.work_item_id = any(%s) and not e.gone and e.removed_at is null"
            " and not c.gone order by e.starts_at", ([i["id"] for i in items],)).fetchall():
        linked.setdefault(r["work_item_id"], []).append(
            {"id": r["id"], "title": r["title"], "start": r["starts_at"].isoformat(), "end": r["ends_at"].isoformat(),
             "all_day": r["all_day"], "color": r["color"]})
    cal_rows = conn.execute(
        "select e.starts_at, e.ends_at from calendar_entry e join calendar c on c.id = e.calendar_id"
        " where c.visible and not c.gone and not e.gone and e.removed_at is null and not e.all_day"
        " and e.show_as = any(%s) and not coalesce((e.attrs ->> 'cancelled')::boolean, false)"
        " and e.starts_at < %s and e.ends_at > %s", (list(BUSY), hi, lo)).fetchall()

    groups: dict[int | None, dict] = {}
    placed = []
    for it in items:
        s, e = _span(it)
        row = {"id": it["id"], "title": it["title"], "status": it["status"], "focus": it["focus"],
               "start_on": it["start_on"], "due": it["due"], "open_ended": bool(s and e is None),
               "milestone": bool(it["due"] and not it["start_on"]), "entries": linked.get(it["id"], [])}
        g = groups.setdefault(it["home_id"], {"items": [], "undated": []})
        if s is None:
            g["undated"].append(row)
            continue
        if s > end or (e is not None and e < start):
            g.setdefault("outside", 0)
            g["outside"] = g["outside"] + 1
            continue
        g["items"].append(row)
        placed.append((s, e, it))

    # Binders: every home in scope, plus binders of the kind (or under the binder) with their own dates here.
    bwhere, bparams = ["not archived", "(starts_on is not null or ends_on is not null)",
                       "coalesce(starts_on, ends_on) <= %(end)s", "coalesce(ends_on, starts_on) >= %(start)s"], \
        {"start": start, "end": end}
    if kind:
        bwhere.append("kind = %(kind)s")
        bparams["kind"] = kind
    if binder is not None:
        bwhere.append("id = any(%(homes)s)")
        bparams["homes"] = params["homes"]
    for r in conn.execute("select id from object where " + " and ".join(bwhere), bparams).fetchall():
        groups.setdefault(r["id"], {"items": [], "undated": []})
    ids = [k for k in groups if k is not None]
    info = {r["id"]: r for r in conn.execute(
        "select id, kind, name, starts_on, ends_on from object where id = any(%s)", (ids,)).fetchall()}
    binders = []
    for oid, g in groups.items():
        o = info.get(oid) if oid is not None else None
        spans = [_span(i) for i in g["items"]]
        firsts = [s for s, _ in spans if s]
        lasts = [e or s for s, e in spans if s]
        b = {"id": oid, "kind": o["kind"] if o else None, "name": o["name"] if o else "No home",
             "starts_on": o["starts_on"] if o else None, "ends_on": o["ends_on"] if o else None,
             "span_start": (o and o["starts_on"]) or (min(firsts) if firsts else None),
             "span_end": (o and o["ends_on"]) or (max(lasts) if lasts else None),
             "derived": not (o and (o["starts_on"] or o["ends_on"])),
             "items": sorted(g["items"], key=lambda i: (_span(i)[0], i["due"] or date.max, i["title"].lower())),
             "undated": sorted(g["undated"], key=lambda i: i["title"].lower()), "outside": g.get("outside", 0)}
        if b["items"] or b["undated"] or not b["derived"]:
            binders.append(b)
    binders.sort(key=lambda b: (b["id"] is None, b["span_start"] is None, b["span_start"] or date.max, b["name"].lower()))

    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    hours = busy_hours(cal_rows, days, zone)
    load = []
    for d in days:
        active = sum(1 for s, e, it in placed if it["status"] != "done" and it["start_on"] and s <= d and (e is None or d <= e))
        due = sum(1 for s, e, it in placed if it["status"] != "done" and it["due"] == d)
        load.append({"day": d, "busy_hours": hours[d], "active": active, "due": due})
    return {"start": start, "end": end, "binders": binders, "load": load,
            "undated": sum(len(b["undated"]) for b in binders)}
