"""The calendar: Talos's own calendars, and copies of the owner's Microsoft 365, iCloud and Google calendars
(docs/calendar.md).

A calendar has a colour (one of the eight series colours, so the legend and the entries agree in
light and dark), a visible switch and the default mark: where a new entry goes. The real calendars
are copied by sync_all over a window of days around today, through read-only sources
(talos.sources.m365calendar, icloudcalendar, googlecalendar). This module never writes to a server:
an entry the owner saves in a real calendar goes there through talos.calwrite, which then stores what the
server answered here (upsert_entry), so the copy and the calendar agree at once.

Times are stored as instants (timestamptz). An all-day entry keeps its dates as midnight UTC with
the end exclusive, as Graph does; the API shows it as start_date and end_date, the end inclusive
(the last day), which is how a form reads.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

log = logging.getLogger("talos.calendars")

COLORS = range(1, 9)
SHOW_AS = ("free", "tentative", "busy", "oof", "workingElsewhere")
BACK_DAYS, AHEAD_DAYS = 60, 400   # the window of real calendars' entries kept in step
SOURCE_NAMES = {"talos": "Talos", "m365": "Microsoft 365", "icloud": "iCloud", "google": "Google"}
MAX_SPAN_DAYS = 400               # the longest range one request may ask for
CURSOR_SCOPE = "calendar"


class CalendarError(ValueError):
    pass


# ---------------------------------------------------------------- calendars

def _next_color(conn: psycopg.Connection) -> int:
    """The first series colour no calendar has yet; the least used when all eight are taken."""
    used = {r["color"]: r["n"] for r in conn.execute(
        "select color, count(*) as n from calendar where not gone group by color").fetchall()}
    return min(COLORS, key=lambda c: (used.get(c, 0), c))


def ensure_talos_calendar(conn: psycopg.Connection) -> None:
    """There is always a Talos calendar to write to, and exactly one default."""
    if not conn.execute("select 1 from calendar where source = 'talos' and not gone").fetchone():
        conn.execute("insert into calendar (source, name, color, is_default, position) values ('talos', 'Talos', %s, true, 0)",
                     (_next_color(conn),))
    if not conn.execute("select 1 from calendar where is_default").fetchone():
        conn.execute("update calendar set is_default = true where id = (select id from calendar where source = 'talos'"
                     " and not gone order by position, id limit 1)")


def _calendar_row(r: dict) -> dict:
    """read_only here means the calendar itself cannot be written (a holiday or subscribed calendar,
    or one shared read-only); whether Talos can write to its source now is talos.calwrite's to add."""
    attrs = r["attrs"] or {}
    return {"id": r["id"], "source": r["source"], "source_name": SOURCE_NAMES[r["source"]], "account_id": r["account_id"],
            "name": r["name"], "color": r["color"], "visible": r["visible"], "is_default": r["is_default"],
            "read_only": r["source"] != "talos" and not attrs.get("can_edit"), "subscribed": bool(attrs.get("subscribed")),
            "owner": attrs.get("owner") or ""}


_SOURCE_ORDER = "array_position(array['talos','m365','icloud','google'], source)"


def list_calendars(conn: psycopg.Connection) -> list[dict]:
    ensure_talos_calendar(conn)
    rows = conn.execute(f"select * from calendar where not gone order by {_SOURCE_ORDER}, position, id").fetchall()
    return [_calendar_row(r) for r in rows]


def _calendar(conn: psycopg.Connection, calendar_id: int) -> dict:
    row = conn.execute("select * from calendar where id = %s and not gone", (calendar_id,)).fetchone()
    if not row:
        raise CalendarError(f"no calendar {calendar_id}")
    return row


def create_calendar(conn: psycopg.Connection, name: str, *, color: int | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise CalendarError("a calendar needs a name")
    if color is not None and color not in COLORS:
        raise CalendarError("the colour must be 1 to 8")
    pos = conn.execute("select coalesce(max(position), 0) + 1 as p from calendar where source = 'talos'").fetchone()["p"]
    row = conn.execute("insert into calendar (source, name, color, position) values ('talos', %s, %s, %s) returning *",
                       (name[:120], color or _next_color(conn), pos)).fetchone()
    return _calendar_row(row)


def update_calendar(conn: psycopg.Connection, calendar_id: int, *, name: str | None = None, color: int | None = None,
                    visible: bool | None = None, is_default: bool | None = None) -> dict:
    """Rename (Talos calendars), recolour, show or hide, or make the default for new entries."""
    cal = _calendar(conn, calendar_id)
    if name is not None:
        if cal["source"] != "talos":
            raise CalendarError(f"a {SOURCE_NAMES[cal['source']]} calendar keeps its own name")
        if not name.strip():
            raise CalendarError("a calendar needs a name")
        conn.execute("update calendar set name = %s, updated_at = now() where id = %s", (name.strip()[:120], calendar_id))
    if color is not None:
        if color not in COLORS:
            raise CalendarError("the colour must be 1 to 8")
        conn.execute("update calendar set color = %s, updated_at = now() where id = %s", (color, calendar_id))
    if visible is not None:
        conn.execute("update calendar set visible = %s, updated_at = now() where id = %s", (bool(visible), calendar_id))
    if is_default:
        # Whether Talos can write to a real calendar is checked by the caller (talos.calwrite.can_write).
        if cal["source"] != "talos" and not (cal["attrs"] or {}).get("can_edit"):
            raise CalendarError(f"{cal['name']} cannot be written to, so it cannot take new entries")
        conn.execute("update calendar set is_default = false where is_default and id <> %s", (calendar_id,))
        conn.execute("update calendar set is_default = true, updated_at = now() where id = %s", (calendar_id,))
    return _calendar_row(_calendar(conn, calendar_id))


# ---------------------------------------------------------------- entries

def _day_utc(d: date) -> datetime:
    return datetime.combine(d, time(0), tzinfo=timezone.utc)


def _entry_row(r: dict) -> dict:
    attrs = r["attrs"] or {}
    out = {"id": r["id"], "calendar_id": r["calendar_id"], "title": r["title"], "all_day": r["all_day"],
           "start": r["starts_at"].isoformat(), "end": r["ends_at"].isoformat(),
           "location": r["location"], "body": r["body"], "show_as": r["show_as"],
           "source": r["source"], "read_only": r["source"] != "talos", "work_item_id": r["work_item_id"],
           "reminder_minutes": r["reminder_minutes"], "recurring": bool(attrs.get("recurring")),
           "body_partial": bool(attrs.get("body_partial")), "is_organizer": attrs.get("is_organizer"),
           "cancelled": bool(attrs.get("cancelled")), "organizer": attrs.get("organizer") or None,
           "attendees": attrs.get("attendees") or [], "attendee_count": attrs.get("attendee_count") or 0,
           "web_link": attrs.get("web_link") or "", "join_url": attrs.get("join_url") or "",
           "response": attrs.get("response") or "", "updated_at": r["updated_at"].isoformat()}
    if r["all_day"]:
        first = r["starts_at"].astimezone(timezone.utc).date()
        last = max(first, r["ends_at"].astimezone(timezone.utc).date() - timedelta(days=1))
        out["start_date"], out["end_date"] = first.isoformat(), last.isoformat()
    return out


ENTRY_SQL = "select e.*, c.source from calendar_entry e join calendar c on c.id = e.calendar_id"


def entries(conn: psycopg.Connection, start: date, end: date, *, calendar_ids: list[int] | None = None) -> list[dict]:
    """Entries that touch the days [start, end] (both inclusive), from calendars that are not gone.

    The window is widened by a day on each side for timed entries: the page places them in its own
    time zone and leaves out what falls outside the days it shows."""
    if end < start:
        raise CalendarError("the end comes before the start")
    if (end - start).days > MAX_SPAN_DAYS:
        raise CalendarError(f"ask for at most {MAX_SPAN_DAYS} days at a time")
    lo, hi = _day_utc(start - timedelta(days=1)), _day_utc(end + timedelta(days=2))
    params: dict[str, Any] = {"lo": lo, "hi": hi, "d0": _day_utc(start), "d1": _day_utc(end + timedelta(days=1))}
    where = ["not e.gone", "e.removed_at is null", "not c.gone",
             "((e.all_day and e.starts_at < %(d1)s and e.ends_at > %(d0)s)"
             " or (not e.all_day and e.starts_at < %(hi)s and e.ends_at >= %(lo)s))"]
    if calendar_ids is not None:
        where.append("e.calendar_id = any(%(cals)s)")
        params["cals"] = list(calendar_ids)
    rows = conn.execute(f"{ENTRY_SQL} where {' and '.join(where)} order by e.all_day desc, e.starts_at, e.ends_at desc, e.id",
                        params).fetchall()
    return [_entry_row(r) for r in rows]


def get_entry(conn: psycopg.Connection, entry_id: int) -> dict | None:
    r = conn.execute(f"{ENTRY_SQL} where e.id = %s and not e.gone and e.removed_at is null", (entry_id,)).fetchone()
    return _entry_row(r) if r else None


def _parse_instant(value: Any, what: str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            raise CalendarError(f"the {what} is not a time") from None
    if dt.tzinfo is None:
        raise CalendarError(f"the {what} needs a time zone")
    return dt


def _parse_day(value: Any, what: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise CalendarError(f"the {what} is not a date") from None


def _times(all_day: bool, start: Any, end: Any) -> tuple[datetime, datetime]:
    """Start and end as stored. All-day: dates, the end inclusive in, exclusive midnight UTC out."""
    if start is None or end is None:
        raise CalendarError("an entry needs a start and an end")
    if all_day:
        d0, d1 = _parse_day(start, "start"), _parse_day(end, "end")
        if d1 < d0:
            raise CalendarError("the end comes before the start")
        return _day_utc(d0), _day_utc(d1 + timedelta(days=1))
    t0, t1 = _parse_instant(start, "start"), _parse_instant(end, "end")
    if t1 < t0:
        raise CalendarError("the end comes before the start")
    if t1 - t0 > timedelta(days=MAX_SPAN_DAYS):
        raise CalendarError("an entry cannot be that long")
    return t0, t1


def _writable(conn: psycopg.Connection, calendar_id: Any) -> dict:
    try:
        cal = _calendar(conn, int(calendar_id))
    except (TypeError, ValueError):
        raise CalendarError("choose a calendar") from None
    if cal["source"] != "talos":
        raise CalendarError(f"{cal['name']} is a Microsoft 365 calendar and read-only here; change it in Outlook")
    return cal


def _text(fields: dict, key: str, limit: int) -> str:
    return str(fields.get(key) or "").strip()[:limit]


def create_entry(conn: psycopg.Connection, *, calendar_id: Any = None, title: str = "", start: Any = None, end: Any = None,
                 all_day: bool = False, location: str = "", body: str = "", show_as: str = "busy",
                 work_item_id: int | None = None, reminder_minutes: Any = None) -> dict:
    """A new entry in a Talos calendar; without calendar_id it goes to the default. A Talos calendar
    keeps an alert's minutes but nothing fires it: alerts are for the real calendars."""
    if calendar_id in (None, ""):
        ensure_talos_calendar(conn)
        calendar_id = conn.execute("select id from calendar where is_default").fetchone()["id"]
    _writable(conn, calendar_id)
    title = (title or "").strip()
    if not title:
        raise CalendarError("an entry needs a title")
    if show_as not in SHOW_AS:
        raise CalendarError(f"show as must be one of {', '.join(SHOW_AS)}")
    if work_item_id is not None and not conn.execute("select 1 from work_item where id = %s", (work_item_id,)).fetchone():
        raise CalendarError(f"no work item {work_item_id}")
    t0, t1 = _times(bool(all_day), start, end)
    eid = conn.execute(
        "insert into calendar_entry (calendar_id, title, starts_at, ends_at, all_day, location, body, show_as, work_item_id,"
        " reminder_minutes) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id",
        (int(calendar_id), title[:500], t0, t1, bool(all_day), str(location or "").strip()[:500],
         str(body or "")[:20000], show_as, work_item_id, reminder(reminder_minutes))).fetchone()["id"]
    return get_entry(conn, eid)


EDITABLE = ("calendar_id", "title", "start", "end", "all_day", "location", "body", "show_as", "reminder_minutes")


def reminder(value: Any) -> int | None:
    """An alert's minutes before the start (0 = at the start), or None for no alert."""
    if value in (None, "", False):
        return None
    try:
        m = int(value)
    except (TypeError, ValueError):
        raise CalendarError("the alert is a number of minutes") from None
    if not 0 <= m <= 40320:
        raise CalendarError("the alert is between 0 minutes and four weeks before")
    return m


def update_entry(conn: psycopg.Connection, entry_id: int, **changes: Any) -> dict:
    """Change a Talos entry. Moving it to another calendar is allowed between Talos calendars."""
    unknown = set(changes) - set(EDITABLE)
    if unknown:
        raise CalendarError(f"cannot change {', '.join(sorted(unknown))}")
    row = conn.execute(f"{ENTRY_SQL} where e.id = %s and not e.gone and e.removed_at is null", (entry_id,)).fetchone()
    if not row:
        raise CalendarError(f"no entry {entry_id}")
    if row["source"] != "talos":
        raise CalendarError("this entry is in a Microsoft 365 calendar and read-only here; change it in Outlook")
    sets: dict[str, Any] = {}
    if "calendar_id" in changes:
        sets["calendar_id"] = int(_writable(conn, changes["calendar_id"])["id"])
    if "title" in changes:
        title = str(changes["title"] or "").strip()
        if not title:
            raise CalendarError("an entry needs a title")
        sets["title"] = title[:500]
    if {"start", "end", "all_day"} & set(changes):
        all_day = bool(changes.get("all_day", row["all_day"]))
        if all_day != row["all_day"] and not {"start", "end"} <= set(changes):
            raise CalendarError("give the start and the end when switching all day on or off")
        cur = _entry_row(row)
        start = changes.get("start", cur["start_date"] if all_day else row["starts_at"])
        end = changes.get("end", cur["end_date"] if all_day else row["ends_at"])
        sets["starts_at"], sets["ends_at"] = _times(all_day, start, end)
        sets["all_day"] = all_day
    if "location" in changes:
        sets["location"] = _text(changes, "location", 500)
    if "body" in changes:
        sets["body"] = str(changes["body"] or "")[:20000]
    if "show_as" in changes:
        if changes["show_as"] not in SHOW_AS:
            raise CalendarError(f"show as must be one of {', '.join(SHOW_AS)}")
        sets["show_as"] = changes["show_as"]
    if "reminder_minutes" in changes:
        sets["reminder_minutes"] = reminder(changes["reminder_minutes"])
    if sets:
        cols = ", ".join(f"{k} = %({k})s" for k in sets)
        conn.execute(f"update calendar_entry set {cols}, updated_at = now() where id = %(id)s", {**sets, "id": entry_id})
    return get_entry(conn, entry_id)


def remove_entry(conn: psycopg.Connection, entry_id: int, *, removed: bool = True) -> None:
    """Remove a Talos entry to the trash (removed_at), or bring it back. Never a hard delete."""
    row = conn.execute(f"{ENTRY_SQL} where e.id = %s and not e.gone", (entry_id,)).fetchone()
    if not row:
        raise CalendarError(f"no entry {entry_id}")
    if row["source"] != "talos":
        raise CalendarError("this entry is in a Microsoft 365 calendar and read-only here; change it in Outlook")
    conn.execute("update calendar_entry set removed_at = case when %s then now() end, updated_at = now() where id = %s",
                 (removed, entry_id))


# ---------------------------------------------------------------- the real calendars

UPSERT_SQL = (
    "insert into calendar_entry (calendar_id, remote_id, title, starts_at, ends_at, all_day, location, body,"
    " show_as, reminder_minutes, attrs) values (%(cid)s, %(remote_id)s, %(title)s, %(starts_at)s, %(ends_at)s, %(all_day)s,"
    " %(location)s, %(body)s, %(show_as)s, %(reminder_minutes)s, %(attrs)s)"
    " on conflict (calendar_id, remote_id) do update set title = excluded.title, starts_at = excluded.starts_at,"
    " ends_at = excluded.ends_at, all_day = excluded.all_day, location = excluded.location, body = excluded.body,"
    " show_as = excluded.show_as, reminder_minutes = excluded.reminder_minutes, attrs = excluded.attrs, gone = false,"
    " removed_at = null,"
    " updated_at = case when (calendar_entry.title, calendar_entry.starts_at, calendar_entry.ends_at, calendar_entry.location,"
    " calendar_entry.reminder_minutes, calendar_entry.attrs) is distinct from (excluded.title, excluded.starts_at,"
    " excluded.ends_at, excluded.location, excluded.reminder_minutes, excluded.attrs) then now()"
    " else calendar_entry.updated_at end returning id")


def upsert_entry(conn: psycopg.Connection, calendar_id: int, fields: dict) -> int:
    """Store one entry as its source gave it (a sync, or the server's answer to a write)."""
    return conn.execute(UPSERT_SQL, {"reminder_minutes": None, **fields, "cid": calendar_id,
                                     "attrs": Jsonb(fields.get("attrs") or {})}).fetchone()["id"]


def sync_source(conn: psycopg.Connection, kind: str, account_id: str, source, *, today: date | None = None) -> dict:
    """Copy one account's calendars of one kind, and their entries from BACK_DAYS ago to AHEAD_DAYS ahead.

    source gives calendar_list() ({remote_id, name, can_edit, hex_color, owner, …}) and
    entries(remote_id, lo, hi) (calendar_entry's columns). New calendars get the next free colour and
    are shown; the owner's choices (colour, visible, default) are kept on later runs. An entry no longer in its
    calendar's window is marked gone, and a calendar no longer listed is marked gone with it. One
    calendar that fails is noted and the others go on."""
    today = today or date.today()
    lo, hi = _day_utc(today - timedelta(days=BACK_DAYS)), _day_utc(today + timedelta(days=AHEAD_DAYS))
    stats = {"calendars": 0, "entries": 0, "gone": 0, "failed": []}
    seen_cals = []
    for i, cal in enumerate(source.calendar_list()):
        attrs = {k: v for k, v in cal.items() if k not in ("remote_id", "name")}
        with conn.transaction():
            row = conn.execute("select id from calendar where source = %s and remote_id = %s", (kind, cal["remote_id"])).fetchone()
            if row:
                cid = row["id"]
                conn.execute("update calendar set name = %s, attrs = %s, gone = false, account_id = %s, position = %s,"
                             " updated_at = now() where id = %s", (cal["name"], Jsonb(attrs), account_id, i, cid))
            else:
                cid = conn.execute(
                    "insert into calendar (source, account_id, remote_id, name, color, position, attrs)"
                    " values (%s, %s, %s, %s, %s, %s, %s) returning id",
                    (kind, account_id, cal["remote_id"], cal["name"], _next_color(conn), i, Jsonb(attrs))).fetchone()["id"]
        seen_cals.append(cid)
        try:
            found = source.entries(cal["remote_id"], lo, hi)
        except Exception as exc:  # noqa: BLE001 — one calendar (a shared one without access) must not stop the rest
            log.warning("calendar %s failed: %s", cal.get("name"), exc)
            stats["failed"].append(cal.get("name") or cal["remote_id"])
            continue
        with conn.transaction():
            ids = [f["remote_id"] for f in found]
            for f in found:
                upsert_entry(conn, cid, f)
            gone = conn.execute(
                "update calendar_entry set gone = true, updated_at = now() where calendar_id = %s and not gone"
                " and remote_id is not null and starts_at < %s and ends_at > %s and not (remote_id = any(%s))",
                (cid, hi, lo, ids)).rowcount
        stats["calendars"] += 1
        stats["entries"] += len(found)
        stats["gone"] += gone
    with conn.transaction():
        conn.execute("update calendar set gone = true, is_default = false, updated_at = now()"
                     " where source = %s and account_id = %s and not gone and not (id = any(%s))", (kind, account_id, seen_cals))
        conn.execute(
            "insert into sync_cursor (account_id, scope, state) values (%s, %s, %s)"
            " on conflict (account_id, scope) do update set state = excluded.state, updated_at = now()",
            (account_id, CURSOR_SCOPE, Jsonb({"synced_at": datetime.now(timezone.utc).isoformat(), "kind": kind, **stats})))
    return stats


def sync_m365(conn: psycopg.Connection, account_id: str, source, *, today: date | None = None) -> dict:
    """A Graph source (calendars() and events() as Graph gives them) copied like any other."""
    from talos.sources.m365calendar import Normalized
    return sync_source(conn, "m365", account_id, Normalized(source), today=today)


def zone(conn: psycopg.Connection):
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(conn.execute("show timezone").fetchone()["TimeZone"])
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def calendar_accounts(conn: psycopg.Connection) -> list[tuple[str, dict]]:
    """(kind, account) for every calendar Talos can read: Microsoft 365 (a Graph account), iCloud (the
    iCloud mail account, whose app password CalDAV takes) and Google (the Gmail account, once signed in
    with talos auth google)."""
    from talos import googleauth
    out = []
    for acct in conn.execute("select * from account where enabled order by id").fetchall():
        s = acct["settings"] or {}
        if acct["provider"] == "graph":
            out.append(("m365", acct))
        elif acct["provider"] == "imap" and s.get("secret") and ((s.get("host") or "").endswith(("mail.me.com", "icloud.com"))
                                                                or acct["address"].lower().endswith(("@icloud.com", "@me.com", "@mac.com"))):
            out.append(("icloud", acct))
        elif acct["provider"] == "gmail" and googleauth.signed_in(acct["id"]):
            out.append(("google", acct))
    return out


def source_for(kind: str, account: dict, *, zone_info=None):
    """The read-only source for one account's calendars."""
    s = account["settings"]
    if kind == "m365":
        from talos.graphauth import GraphAuth
        from talos.sources.graph import GraphClient
        from talos.sources.m365calendar import M365CalendarSource
        auth = GraphAuth(account["id"], s["tenant_id"], s["client_id"])
        return M365CalendarSource(GraphClient(auth.token, refresh=lambda: auth.token(force_refresh=True)))
    if kind == "icloud":
        from talos import secrets
        from talos.sources.icloudcalendar import ICloudCalendarSource
        return ICloudCalendarSource(s.get("username") or account["address"], lambda: secrets.get(s["secret"]), zone=zone_info)
    if kind == "google":
        from talos.googleauth import GoogleAuth
        from talos.sources.googlecalendar import GoogleCalendarSource
        return GoogleCalendarSource(GoogleAuth(account["id"]).token)
    raise CalendarError(f"no calendar source for {kind}")


def sync_all(conn: psycopg.Connection, *, source_factory=None) -> dict:
    """Every calendar account; one that fails is reported, never raised. source_factory(kind, account)
    replaces the network in tests (a factory taking only the account is taken to be Microsoft 365's)."""
    import inspect
    out = {}
    z = zone(conn)
    for kind, acct in calendar_accounts(conn):
        try:
            if source_factory is None:
                src = source_for(kind, acct, zone_info=z)
            elif len(inspect.signature(source_factory).parameters) == 1:
                if kind != "m365":
                    continue
                src = source_factory(acct)
            else:
                src = source_factory(kind, acct)
            if kind == "m365" and not hasattr(src, "calendar_list"):
                out[acct["id"]] = sync_m365(conn, acct["id"], src)
            else:
                out[acct["id"] if kind == "m365" else f"{kind}:{acct['id']}"] = sync_source(conn, kind, acct["id"], src)
            conn.commit()
        except Exception as exc:  # noqa: BLE001 — the calendar copy never fails a mail sync
            conn.rollback()
            log.exception("calendar sync failed kind=%s account=%s", kind, acct["id"])
            out[acct["id"] if kind == "m365" else f"{kind}:{acct['id']}"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    return out


def last_sync(conn: psycopg.Connection) -> dict | None:
    row = conn.execute("select account_id, state, updated_at from sync_cursor where scope = %s order by updated_at desc limit 1",
                       (CURSOR_SCOPE,)).fetchone()
    return {"account_id": row["account_id"], "at": row["updated_at"].isoformat(), **(row["state"] or {})} if row else None


def syncs(conn: psycopg.Connection) -> dict[str, dict]:
    """The last copy per source kind: when, and what failed."""
    out = {}
    for r in conn.execute("select account_id, state, updated_at from sync_cursor where scope = %s order by updated_at",
                          (CURSOR_SCOPE,)).fetchall():
        st = r["state"] or {}
        out[st.get("kind", "m365")] = {"at": r["updated_at"].isoformat(), "failed": st.get("failed") or [],
                                       "entries": st.get("entries", 0)}
    return out
