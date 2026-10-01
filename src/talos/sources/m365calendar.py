"""Microsoft 365 calendars over Microsoft Graph, read-only (writing is talos.calwrite's).

The calendar list (/me/calendars) and, per calendar, its calendar view for a window of days
(/me/calendars/{id}/calendarView): Graph expands recurring series into their occurrences there,
so each occurrence is one entry with its own id. Times come back in UTC, since the client asks
for no other time zone. Only GET is sent, with the Calendars.Read scope already in the read-only
SCOPES; talos.calendars stores what this returns.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

from talos.sources.graph import GraphClient

CALENDAR_SELECT = "id,name,color,hexColor,canEdit,isDefaultCalendar,owner"
EVENT_SELECT = ("id,subject,start,end,isAllDay,location,showAs,isCancelled,organizer,webLink,bodyPreview,"
                "attendees,isOnlineMeeting,onlineMeeting,sensitivity,type,responseStatus,isOrganizer,isReminderOn,"
                "reminderMinutesBeforeStart")
PAGE_SIZE = 200
MAX_PAGES = 50  # 10,000 entries per calendar and window: far beyond a real calendar


def _utc(value: str) -> datetime:
    """Graph's '2026-09-29T10:00:00.0000000' (seven decimals, zone given apart) as UTC."""
    return datetime.fromisoformat(value[:19]).replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class M365CalendarSource:
    def __init__(self, client: GraphClient):
        self.client = client

    def _pages(self, url: str) -> list[dict]:
        out: list[dict] = []
        for _ in range(MAX_PAGES):
            page = self.client.get(url, page_size=PAGE_SIZE)
            out.extend(page.get("value", []))
            url = page.get("@odata.nextLink")
            if not url:
                break
        return out

    def calendars(self) -> list[dict]:
        return self._pages(f"/me/calendars?$select={CALENDAR_SELECT}")

    def calendar_list(self) -> list[dict]:
        return Normalized(self).calendar_list()

    def entries(self, remote_id: str, lo: datetime, hi: datetime) -> list[dict]:
        return Normalized(self).entries(remote_id, lo, hi)

    def events(self, calendar_id: str, start: datetime, end: datetime) -> list[dict]:
        """The calendar's entries that overlap [start, end), recurring ones expanded."""
        return self._pages(f"/me/calendars/{quote(calendar_id, safe='')}/calendarView"
                           f"?startDateTime={_iso(start)}&endDateTime={_iso(end)}&$select={EVENT_SELECT}")


class Normalized:
    """Any source with Graph's calendars() and events() in the shape every calendar source gives
    (talos.calendars.sync_source): calendar_list() and entries()."""

    def __init__(self, raw):
        self.raw = raw

    def calendar_list(self) -> list[dict]:
        return [{"remote_id": c["id"], "name": c.get("name") or "Calendar", "can_edit": bool(c.get("canEdit")),
                 "hex_color": c.get("hexColor") or "", "owner": ((c.get("owner") or {}).get("address") or ""),
                 "outlook_default": bool(c.get("isDefaultCalendar"))} for c in self.raw.calendars()]

    def entries(self, remote_id: str, lo: datetime, hi: datetime) -> list[dict]:
        return [entry_fields(ev) for ev in self.raw.events(remote_id, lo, hi)]


PREVIEW_MAX = 255  # Graph's bodyPreview is cut at this length


def entry_fields(ev: dict) -> dict:
    """One Graph event as calendar_entry's columns. All-day dates stay midnight UTC (end exclusive)."""
    attendees = [{"name": ((a.get("emailAddress") or {}).get("name") or ""),
                  "address": ((a.get("emailAddress") or {}).get("address") or ""),
                  "type": a.get("type") or "", "response": ((a.get("status") or {}).get("response") or "")}
                 for a in (ev.get("attendees") or [])]
    organizer = (ev.get("organizer") or {}).get("emailAddress") or {}
    attrs = {"organizer": {"name": organizer.get("name") or "", "address": organizer.get("address") or ""},
             "attendees": attendees[:100], "attendee_count": len(attendees),
             "cancelled": bool(ev.get("isCancelled")), "web_link": ev.get("webLink") or "",
             "online": bool(ev.get("isOnlineMeeting")),
             "join_url": ((ev.get("onlineMeeting") or {}).get("joinUrl") or ""),
             "sensitivity": ev.get("sensitivity") or "", "type": ev.get("type") or "",
             "recurring": (ev.get("type") or "singleInstance") != "singleInstance",
             "is_organizer": bool(ev.get("isOrganizer", True)),
             "body_partial": len(ev.get("bodyPreview") or "") >= PREVIEW_MAX - 5,
             "response": ((ev.get("responseStatus") or {}).get("response") or "")}
    return {"remote_id": ev["id"], "title": (ev.get("subject") or "").strip() or "(no title)",
            "starts_at": _utc(ev["start"]["dateTime"]), "ends_at": _utc(ev["end"]["dateTime"]),
            "all_day": bool(ev.get("isAllDay")),
            "location": ((ev.get("location") or {}).get("displayName") or ""),
            "body": ev.get("bodyPreview") or "", "show_as": ev.get("showAs") or "busy",
            "reminder_minutes": ev.get("reminderMinutesBeforeStart") if ev.get("isReminderOn") else None, "attrs": attrs}
