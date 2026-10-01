"""Writing calendar entries to the owner's real calendars: Microsoft 365, iCloud and Google (docs/calendar.md).

An entry the owner creates, edits or removes in Talos Web goes to the real calendar at once; the server's answer
is stored as the copy (calendars.upsert_entry), so Talos and the calendar agree without waiting for a
sync. Only the web app imports this module: no sync, rule, job or model ever writes to a calendar.

What it will not do, whatever it is asked:
- **Never invite.** No write names an attendee. An entry that has invitees other than the owner stays
  read-only here, since changing or removing it would send them an update or a cancellation; Google
  writes carry sendUpdates=none besides.
- **Never touch a series.** An occurrence of a repeating event is read-only here: change the series
  in the calendar's own app.
- **Trash is the furthest.** Removing an entry sends it to Outlook's Deleted Items or Google's trash.
  iCloud has no trash for a single event, so an iCloud entry is not removed from here.
- **Only calendars the owner may write** (canEdit, accessRole, the CalDAV write privilege), and for Microsoft
  365 only once the Calendars.ReadWrite permission is granted (CALENDAR_SCOPES, asked for by name).
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx
import icalendar
import psycopg

from talos import calendars
from talos.calendars import CalendarError

APP = {"m365": "Outlook", "icloud": "Calendar on your Mac or iPhone", "google": "Google Calendar"}
READY_TTL = 300
_ready_cache: dict[str, tuple[float, bool, str | None]] = {}


class WriteRefused(CalendarError):
    pass


# ---------------------------------------------------------------- may this be written?

def my_addresses(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute("select address from my_address union select address from account").fetchall()
    return {r["address"].lower() for r in rows if r["address"]}


def lock_reason(source: str, cal_attrs: dict, entry: dict | None, me: set[str]) -> str | None:
    """Why an entry (or a calendar, with entry None) cannot be written from Talos, or None."""
    if source == "talos":
        return None
    app = APP[source]
    if not (cal_attrs or {}).get("can_edit"):
        return "this calendar is read-only"
    if entry is None:
        return None
    attrs = entry.get("attrs") or {}
    if attrs.get("cancelled"):
        return f"it is cancelled; it goes away in {app}"
    if attrs.get("recurring"):
        return f"it is one of a repeating series; change the series in {app}"
    others = [a for a in attrs.get("attendees") or [] if (a.get("address") or "").lower() not in me and not a.get("self")]
    if others:
        return f"it has invitees, who would be sent an update; change it in {app}"
    if source == "m365" and attrs.get("is_organizer") is False:
        return f"it is someone else's meeting; answer it in {app}"
    return None


def ready(conn: psycopg.Connection, kind: str, account: dict | None = None) -> tuple[bool, str | None]:
    """Whether Talos can write to this kind of calendar now, and if not, what the owner can do about it.
    Asked at most every READY_TTL seconds (the Microsoft check asks MSAL's cache)."""
    if kind == "talos":
        return True, None
    hit = _ready_cache.get(kind)
    if hit and time.monotonic() - hit[0] < READY_TTL:
        return hit[1], hit[2]
    ok, why = _ready_now(conn, kind, account)
    _ready_cache[kind] = (time.monotonic(), ok, why)
    return ok, why


def _account(conn: psycopg.Connection, kind: str) -> dict | None:
    return next((a for k, a in calendars.calendar_accounts(conn) if k == kind), None)


def _ready_now(conn: psycopg.Connection, kind: str, account: dict | None) -> tuple[bool, str | None]:
    from talos import secrets
    account = account or _account(conn, kind)
    if account is None:
        return False, "not connected"
    s = account["settings"] or {}
    if kind == "m365":
        from talos.graphauth import CALENDAR_SCOPES, GraphAuth
        if not s.get("tenant_id") or not secrets.exists(f"graph-token-cache:{account['id']}"):
            return False, f"not signed in to Microsoft: talos auth graph {account['id']}"
        if GraphAuth(account["id"], s["tenant_id"], s["client_id"]).granted(CALENDAR_SCOPES[0]):
            return True, None
        return False, ("writing needs the Calendars.ReadWrite permission: add it to the Talos app in Entra, then"
                       f" sign in again with talos auth graph {account['id']} (docs/calendar.md)")
    if kind == "icloud":
        return (True, None) if secrets.exists(s["secret"]) else (False, "the iCloud app password is not in the Keychain")
    if kind == "google":
        from talos import googleauth
        return (True, None) if googleauth.signed_in(account["id"]) else (False, "sign in with talos auth google")
    return False, "unknown calendar"


def annotate(conn: psycopg.Connection, cals: list[dict], entries: list[dict], readiness: dict | None = None) -> None:
    """Add to the page's calendars whether they can be written (writable, write_note) and to its entries
    whether they can be edited here (read_only, read_only_reason, removable)."""
    raw = {r["id"]: r for r in conn.execute("select id, source, attrs from calendar where id = any(%s)",
                                           ([c["id"] for c in cals],)).fetchall()}
    me = my_addresses(conn)
    state = {}
    for kind in {c["source"] for c in cals}:
        state[kind] = (readiness or {}).get(kind) or ready(conn, kind)
    for c in cals:
        ok, why = state[c["source"]]
        lock = lock_reason(c["source"], raw[c["id"]]["attrs"], None, me)
        c["writable"] = bool(ok and not lock)
        c["write_note"] = lock or (None if ok else why)
    by_cal = {c["id"]: c for c in cals}
    for e in entries:
        c = by_cal.get(e["calendar_id"])
        if c is None:
            continue
        if not c["writable"]:
            e["read_only"], e["read_only_reason"] = True, c["write_note"]
        else:
            why = lock_reason(c["source"], raw[c["id"]]["attrs"], {"attrs": e}, me)
            e["read_only"], e["read_only_reason"] = bool(why), why
        e["removable"] = not e["read_only"] and c["source"] != "icloud"
        e["undoable"] = c["source"] == "talos"


def can_write(conn: psycopg.Connection, calendar_id: int) -> bool:
    cal = calendars._calendar(conn, calendar_id)
    ok, _ = ready(conn, cal["source"])
    return ok and lock_reason(cal["source"], cal["attrs"], None, set()) is None


# ---------------------------------------------------------------- the writers

def _utc(t: datetime) -> datetime:
    return t.astimezone(timezone.utc)


class M365Writer:
    """Graph: POST /me/calendars/{id}/events, PATCH /me/events/{id}, DELETE /me/events/{id} (to Deleted Items)."""

    GRAPH = "https://graph.microsoft.com/v1.0"
    HEADERS = {"Prefer": 'IdType="ImmutableId", outlook.timezone="UTC"', "Content-Type": "application/json"}

    def __init__(self, token, http: httpx.Client | None = None):
        self.token = token
        self.http = http or httpx.Client(timeout=60)

    def _send(self, method: str, url: str, body: dict | None = None) -> dict | None:
        r = self.http.request(method, self.GRAPH + url, json=body, headers={**self.HEADERS, "Authorization": f"Bearer {self.token()}"})
        if r.status_code >= 300:
            raise CalendarError(f"Outlook refused it ({r.status_code}): {r.text[:200]}")
        return r.json() if r.content else None

    @staticmethod
    def _body(p: dict, keys: set[str]) -> dict:
        out: dict[str, Any] = {}
        if "title" in keys:
            out["subject"] = p["title"]
        if keys & {"start", "end", "all_day"}:
            fmt = "%Y-%m-%dT%H:%M:%S"
            out["isAllDay"] = p["all_day"]
            out["start"] = {"dateTime": _utc(p["starts_at"]).strftime(fmt), "timeZone": "UTC"}
            out["end"] = {"dateTime": _utc(p["ends_at"]).strftime(fmt), "timeZone": "UTC"}
        if "location" in keys:
            out["location"] = {"displayName": p["location"]}
        if "body" in keys:
            out["body"] = {"contentType": "text", "content": p["body"]}
        if "show_as" in keys:
            out["showAs"] = p["show_as"]
        if "reminder_minutes" in keys:
            out["isReminderOn"] = p["reminder_minutes"] is not None
            out["reminderMinutesBeforeStart"] = p["reminder_minutes"] or 0
        return out

    def create(self, cal_remote: str, p: dict) -> dict:
        from talos.sources.m365calendar import entry_fields
        return entry_fields(self._send("POST", f"/me/calendars/{quote(cal_remote, safe='')}/events", self._body(p, set(p))))

    def update(self, cal_remote: str, remote_id: str, attrs: dict, p: dict, changed: set[str]) -> dict:
        from talos.sources.m365calendar import entry_fields
        return entry_fields(self._send("PATCH", f"/me/events/{quote(remote_id, safe='')}", self._body(p, changed)))

    def remove(self, cal_remote: str, remote_id: str, attrs: dict) -> None:
        self._send("DELETE", f"/me/events/{quote(remote_id, safe='')}")


class GoogleWriter:
    """Calendar API v3: insert, patch and delete (to Google's trash), always with sendUpdates=none."""

    API = "https://www.googleapis.com/calendar/v3"

    def __init__(self, token, http: httpx.Client | None = None):
        self.token = token
        self.http = http or httpx.Client(timeout=60)

    def _send(self, method: str, path: str, body: dict | None = None) -> dict | None:
        r = self.http.request(method, f"{self.API}{path}", params={"sendUpdates": "none"}, json=body,
                              headers={"Authorization": f"Bearer {self.token()}"})
        if r.status_code >= 300 and not (method == "DELETE" and r.status_code == 410):
            raise CalendarError(f"Google refused it ({r.status_code}): {r.text[:200]}")
        return r.json() if r.content else None

    @staticmethod
    def _body(p: dict, keys: set[str]) -> dict:
        out: dict[str, Any] = {}
        if "title" in keys:
            out["summary"] = p["title"]
        if keys & {"start", "end", "all_day"}:
            if p["all_day"]:
                out["start"] = {"date": _utc(p["starts_at"]).date().isoformat()}
                out["end"] = {"date": _utc(p["ends_at"]).date().isoformat()}
            else:
                out["start"] = {"dateTime": _utc(p["starts_at"]).isoformat()}
                out["end"] = {"dateTime": _utc(p["ends_at"]).isoformat()}
        if "location" in keys:
            out["location"] = p["location"]
        if "body" in keys:
            out["description"] = p["body"]
        if "show_as" in keys:
            out["transparency"] = "transparent" if p["show_as"] == "free" else "opaque"
        if "reminder_minutes" in keys:
            m = p["reminder_minutes"]
            out["reminders"] = {"useDefault": False, "overrides": [] if m is None else [{"method": "popup", "minutes": m}]}
        return out

    def create(self, cal_remote: str, p: dict) -> dict:
        from talos.sources.googlecalendar import event_fields
        return event_fields(self._send("POST", f"/calendars/{quote(cal_remote, safe='')}/events", self._body(p, set(p))))

    def update(self, cal_remote: str, remote_id: str, attrs: dict, p: dict, changed: set[str]) -> dict:
        from talos.sources.googlecalendar import event_fields
        return event_fields(self._send("PATCH", f"/calendars/{quote(cal_remote, safe='')}/events/{quote(remote_id, safe='')}",
                                       self._body(p, changed)))

    def remove(self, cal_remote: str, remote_id: str, attrs: dict) -> None:
        self._send("DELETE", f"/calendars/{quote(cal_remote, safe='')}/events/{quote(remote_id, safe='')}")


class ICloudWriter:
    """CalDAV: a new event is PUT as <uid>.ics (If-None-Match: *); an edit reads the event, changes the
    fields in its VEVENT and PUTs it back with If-Match on its ETag, so a change made meanwhile on the
    phone is never overwritten. There is no remove: iCloud has no trash for one event."""

    def __init__(self, username: str, password, http: httpx.Client | None = None, *, zone=None):
        self.username, self.password = username, password
        self.http = http or httpx.Client(timeout=60, follow_redirects=True)
        self.zone = zone

    def _auth(self):
        return (self.username, self.password())

    def _read(self, href: str) -> tuple[icalendar.Calendar, str | None]:
        r = self.http.get(href, auth=self._auth())
        if r.status_code != 200:
            raise CalendarError(f"iCloud gave no event at {href.rsplit('/', 1)[-1]} ({r.status_code})")
        return icalendar.Calendar.from_ical(r.content), r.headers.get("ETag")

    def _fields(self, href: str) -> dict:
        from talos.sources.icloudcalendar import parse_calendar_data
        cal, etag = self._read(href)
        found = parse_calendar_data(cal.to_ical().decode(), href, etag, self.zone or calendars_zone())
        if not found:
            raise CalendarError("iCloud stored the event, but it reads back empty")
        return found[0]

    @staticmethod
    def _apply(ev: icalendar.Event, p: dict, keys: set[str]) -> None:
        def put(name, value):
            if name in ev:
                del ev[name]
            if value not in (None, ""):
                ev.add(name, value)
        if "title" in keys:
            put("SUMMARY", p["title"])
        if keys & {"start", "end", "all_day"}:
            for k in ("DTSTART", "DTEND", "DURATION"):
                if k in ev:
                    del ev[k]
            if p["all_day"]:
                ev.add("DTSTART", _utc(p["starts_at"]).date())
                ev.add("DTEND", _utc(p["ends_at"]).date())
            else:
                ev.add("DTSTART", _utc(p["starts_at"]))
                ev.add("DTEND", _utc(p["ends_at"]))
        if "location" in keys:
            put("LOCATION", p["location"])
        if "body" in keys:
            put("DESCRIPTION", p["body"])
        if "show_as" in keys:
            put("TRANSP", "TRANSPARENT" if p["show_as"] == "free" else "OPAQUE")
        if "reminder_minutes" in keys:
            ev.subcomponents = [c for c in ev.subcomponents if c.name != "VALARM"]
            if p["reminder_minutes"] is not None:
                alarm = icalendar.Alarm()
                alarm.add("ACTION", "DISPLAY")
                alarm.add("DESCRIPTION", p["title"] or "Reminder")
                alarm.add("TRIGGER", timedelta(minutes=-p["reminder_minutes"]))
                ev.add_component(alarm)
        now = datetime.now(timezone.utc)
        put("DTSTAMP", now)
        put("LAST-MODIFIED", now)

    def create(self, cal_remote: str, p: dict) -> dict:
        uid = str(uuid.uuid4()).upper()
        cal = icalendar.Calendar()
        cal.add("PRODID", "-//Talos//Calendar//EN")
        cal.add("VERSION", "2.0")
        ev = icalendar.Event()
        ev.add("UID", uid)
        ev.add("CREATED", datetime.now(timezone.utc))
        ev.add("SEQUENCE", 0)
        self._apply(ev, p, set(p))
        cal.add_component(ev)
        href = cal_remote.rstrip("/") + f"/{uid}.ics"
        r = self.http.put(href, content=cal.to_ical(), auth=self._auth(),
                          headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"})
        if r.status_code not in (200, 201, 204):
            raise CalendarError(f"iCloud refused the new event ({r.status_code}): {r.text[:200]}")
        return self._fields(href)

    def update(self, cal_remote: str, remote_id: str, attrs: dict, p: dict, changed: set[str]) -> dict:
        href = attrs.get("href") or remote_id
        cal, etag = self._read(href)
        masters = [e for e in cal.walk("VEVENT") if e.get("RECURRENCE-ID") is None]
        if len(masters) != 1 or masters[0].get("RRULE") is not None:
            raise WriteRefused("it is one of a repeating series; change the series in Calendar")
        ev = masters[0]
        self._apply(ev, p, changed)
        ev["SEQUENCE"] = int(ev.get("SEQUENCE", 0)) + 1
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if etag:
            headers["If-Match"] = etag
        r = self.http.put(href, content=cal.to_ical(), auth=self._auth(), headers=headers)
        if r.status_code == 412:
            raise CalendarError("it was changed elsewhere meanwhile; Talos has not overwritten it. Refresh and try again")
        if r.status_code not in (200, 201, 204):
            raise CalendarError(f"iCloud refused the change ({r.status_code}): {r.text[:200]}")
        return self._fields(href)

    def remove(self, cal_remote: str, remote_id: str, attrs: dict) -> None:
        raise WriteRefused("iCloud has no trash for a single event, so Talos does not remove it; remove it in Calendar")


def calendars_zone():
    from zoneinfo import ZoneInfo
    return ZoneInfo("Europe/Stockholm")


def writer_for(conn: psycopg.Connection, kind: str, account: dict):
    s = account["settings"]
    if kind == "m365":
        from talos.graphauth import CALENDAR_SCOPES, GraphAuth
        auth = GraphAuth(account["id"], s["tenant_id"], s["client_id"])
        return M365Writer(lambda: auth.token(scopes=CALENDAR_SCOPES))
    if kind == "google":
        from talos.googleauth import GoogleAuth
        return GoogleWriter(GoogleAuth(account["id"]).token)
    if kind == "icloud":
        from talos import secrets
        return ICloudWriter(s.get("username") or account["address"], lambda: secrets.get(s["secret"]), zone=calendars.zone(conn))
    raise CalendarError(f"no writer for {kind}")


# ---------------------------------------------------------------- what the web app calls

def _writer(conn: psycopg.Connection, cal: dict, writers: dict | None):
    if writers is not None:
        return writers[cal["source"]]
    account = conn.execute("select * from account where id = %s", (cal["account_id"],)).fetchone()
    ok, why = ready(conn, cal["source"], account)
    if not ok:
        raise WriteRefused(f"{cal['name']}: {why}")
    return writer_for(conn, cal["source"], account)


def _payload(title, t0, t1, all_day, location, body, show_as, reminder_minutes) -> dict:
    return {"title": title, "starts_at": t0, "ends_at": t1, "all_day": all_day, "location": location,
            "body": body, "show_as": show_as, "reminder_minutes": reminder_minutes}


def create_entry(conn: psycopg.Connection, *, writers: dict | None = None, **fields: Any) -> dict:
    """A new entry: in a Talos calendar as before, in a real calendar through its server first."""
    calendar_id = fields.get("calendar_id")
    if calendar_id in (None, ""):
        calendars.ensure_talos_calendar(conn)
        calendar_id = conn.execute("select id from calendar where is_default").fetchone()["id"]
    try:
        cal = calendars._calendar(conn, int(calendar_id))
    except (TypeError, ValueError):
        raise CalendarError("choose a calendar") from None
    if cal["source"] == "talos":
        return calendars.create_entry(conn, **{**fields, "calendar_id": cal["id"]})
    why = lock_reason(cal["source"], cal["attrs"], None, set())
    if why:
        raise WriteRefused(f"{cal['name']}: {why}")
    title = str(fields.get("title") or "").strip()[:500]
    if not title:
        raise CalendarError("an entry needs a title")
    show_as = fields.get("show_as") or "busy"
    if show_as not in calendars.SHOW_AS:
        raise CalendarError(f"show as must be one of {', '.join(calendars.SHOW_AS)}")
    work_item_id = fields.get("work_item_id")
    if work_item_id is not None and not conn.execute("select 1 from work_item where id = %s", (work_item_id,)).fetchone():
        raise CalendarError(f"no work item {work_item_id}")
    all_day = bool(fields.get("all_day"))
    t0, t1 = calendars._times(all_day, fields.get("start"), fields.get("end"))
    p = _payload(title, t0, t1, all_day, str(fields.get("location") or "").strip()[:500], str(fields.get("body") or "")[:20000],
                 show_as, calendars.reminder(fields.get("reminder_minutes")))
    got = _writer(conn, cal, writers).create(cal["remote_id"], p)
    eid = calendars.upsert_entry(conn, cal["id"], got)
    if work_item_id is not None:
        conn.execute("update calendar_entry set work_item_id = %s where id = %s", (work_item_id, eid))
    return calendars.get_entry(conn, eid)


def update_entry(conn: psycopg.Connection, entry_id: int, *, writers: dict | None = None, **changes: Any) -> dict:
    """Save the owner's change to an entry. A Talos entry changes in Talos alone. An entry of a calendar that
    lives elsewhere (Microsoft 365, iCloud, Google) is written there first, and Talos's copy is then
    taken from what the server answered, so the copy never claims a change the server refused.
    Refused, with the reason, where the calendar or entry is locked (lock_reason: someone else's
    meeting, a read-only calendar), where notes are longer than Talos's copy (Outlook keeps the
    rest), and for moves between calendars, which are made as a new entry there instead."""
    row = conn.execute(f"{calendars.ENTRY_SQL} where e.id = %s and not e.gone and e.removed_at is null", (entry_id,)).fetchone()
    if not row:
        raise CalendarError(f"no entry {entry_id}")
    if row["source"] == "talos":
        target = changes.get("calendar_id")
        if target not in (None, "", row["calendar_id"]) and calendars._calendar(conn, int(target))["source"] != "talos":
            raise WriteRefused("a Talos entry stays in Talos; make a new entry in the other calendar")
        return calendars.update_entry(conn, entry_id, **changes)
    unknown = set(changes) - set(calendars.EDITABLE)
    if unknown:
        raise CalendarError(f"cannot change {', '.join(sorted(unknown))}")
    cal = calendars._calendar(conn, row["calendar_id"])
    why = lock_reason(cal["source"], cal["attrs"], {"attrs": row["attrs"]}, my_addresses(conn))
    if why:
        raise WriteRefused(why)
    if changes.get("calendar_id") not in (None, "", row["calendar_id"]) and int(changes["calendar_id"]) != row["calendar_id"]:
        raise WriteRefused("moving an entry to another calendar is not done from here; make it anew there")
    cur = calendars._entry_row(row)
    if "body" in changes and (row["attrs"] or {}).get("body_partial") and str(changes["body"] or "") != row["body"]:
        raise WriteRefused("its notes are longer than Talos's copy shows; change them in Outlook")
    all_day = bool(changes.get("all_day", row["all_day"]))
    if {"start", "end", "all_day"} & set(changes):
        if all_day != row["all_day"] and not {"start", "end"} <= set(changes):
            raise CalendarError("give the start and the end when switching all day on or off")
        start = changes.get("start", cur.get("start_date") if all_day else row["starts_at"])
        end = changes.get("end", cur.get("end_date") if all_day else row["ends_at"])
        t0, t1 = calendars._times(all_day, start, end)
    else:
        t0, t1 = row["starts_at"], row["ends_at"]
    title = str(changes.get("title", row["title"]) or "").strip()[:500]
    if not title:
        raise CalendarError("an entry needs a title")
    show_as = changes.get("show_as", row["show_as"])
    if show_as not in calendars.SHOW_AS:
        raise CalendarError(f"show as must be one of {', '.join(calendars.SHOW_AS)}")
    p = _payload(title, t0, t1, all_day, str(changes.get("location", row["location"]) or "").strip()[:500],
                 str(changes.get("body", row["body"]) or "")[:20000], show_as,
                 calendars.reminder(changes["reminder_minutes"]) if "reminder_minutes" in changes else row["reminder_minutes"])
    was = {"title": row["title"], "location": row["location"], "body": row["body"], "show_as": row["show_as"],
           "reminder_minutes": row["reminder_minutes"]}
    changed = {k for k in was if p[k] != was[k]}
    if (t0, t1, all_day) != (row["starts_at"], row["ends_at"], row["all_day"]):
        changed |= {"start", "end", "all_day"}
    if not changed:
        return calendars.get_entry(conn, entry_id)
    got = _writer(conn, cal, writers).update(cal["remote_id"], row["remote_id"], row["attrs"] or {}, p, changed)
    if got["remote_id"] != row["remote_id"]:
        conn.execute("update calendar_entry set remote_id = %s where id = %s", (got["remote_id"], entry_id))
    calendars.upsert_entry(conn, cal["id"], got)
    return calendars.get_entry(conn, entry_id)


def remove_entry(conn: psycopg.Connection, entry_id: int, *, removed: bool = True, writers: dict | None = None) -> None:
    row = conn.execute(f"{calendars.ENTRY_SQL} where e.id = %s and not e.gone", (entry_id,)).fetchone()
    if not row:
        raise CalendarError(f"no entry {entry_id}")
    if row["source"] == "talos":
        return calendars.remove_entry(conn, entry_id, removed=removed)
    app = APP[row["source"]]
    if not removed:
        raise WriteRefused(f"it cannot be brought back from here; restore it from the deleted items in {app}")
    cal = calendars._calendar(conn, row["calendar_id"])
    why = lock_reason(cal["source"], cal["attrs"], {"attrs": row["attrs"]}, my_addresses(conn))
    if why:
        raise WriteRefused(why)
    if cal["source"] == "icloud":
        raise WriteRefused("iCloud has no trash for a single event, so Talos does not remove it; remove it in Calendar")
    _writer(conn, cal, writers).remove(cal["remote_id"], row["remote_id"], row["attrs"] or {})
    conn.execute("update calendar_entry set removed_at = now(), gone = true, updated_at = now() where id = %s", (entry_id,))
