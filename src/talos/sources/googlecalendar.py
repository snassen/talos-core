"""Google calendars over the Calendar API (v3), read-only.

The calendar list (users/me/calendarList) and, per calendar, its events in a window with
singleEvents=true, so Google expands recurring series into their occurrences. Only GET is sent;
writing is talos.calwrite's. The token comes from talos.googleauth.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Callable
from urllib.parse import quote

import httpx

API = "https://www.googleapis.com/calendar/v3"
MAX_PAGES = 40


class GoogleCalendarError(RuntimeError):
    pass


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(v: dict) -> tuple[datetime, bool]:
    if "date" in v:
        return datetime.combine(date.fromisoformat(v["date"]), time(0), tzinfo=timezone.utc), True
    return datetime.fromisoformat(v["dateTime"].replace("Z", "+00:00")), False


def event_fields(ev: dict) -> dict:
    """One Google event (an occurrence) as calendar_entry's columns."""
    start, all_day = _when(ev["start"])
    end, _ = _when(ev.get("end") or ev["start"])
    rem = ev.get("reminders") or {}
    popups = [o.get("minutes") for o in rem.get("overrides") or [] if o.get("method") == "popup"]
    attendees = [{"name": a.get("displayName") or "", "address": a.get("email") or "",
                  "type": "optional" if a.get("optional") else "required", "response": a.get("responseStatus") or "",
                  "self": bool(a.get("self"))} for a in ev.get("attendees") or []]
    org = ev.get("organizer") or {}
    attrs = {"organizer": {"name": org.get("displayName") or "", "address": org.get("email") or "", "self": bool(org.get("self"))},
             "attendees": attendees[:100], "attendee_count": len(attendees),
             "cancelled": ev.get("status") == "cancelled",
             "recurring": bool(ev.get("recurringEventId") or ev.get("recurrence")),
             "web_link": ev.get("htmlLink") or "", "join_url": ev.get("hangoutLink") or "",
             "response": next((a["response"] for a in attendees if a["self"]), ""), "etag": ev.get("etag")}
    return {"remote_id": ev["id"], "title": (ev.get("summary") or "").strip() or "(no title)",
            "starts_at": start, "ends_at": max(end, start), "all_day": all_day,
            "location": (ev.get("location") or "").strip(), "body": (ev.get("description") or "")[:20000],
            "show_as": "free" if ev.get("transparency") == "transparent" else "busy",
            "reminder_minutes": popups[0] if popups else None, "attrs": attrs}


class GoogleCalendarSource:
    def __init__(self, token: Callable[[], str], http: httpx.Client | None = None):
        self.token = token
        self.http = http or httpx.Client(timeout=60)

    def _get(self, url: str, params: dict | None = None) -> dict:
        r = self.http.get(url, params=params, headers={"Authorization": f"Bearer {self.token()}"})
        if r.status_code != 200:
            raise GoogleCalendarError(f"GET {url.split('?')[0]} -> {r.status_code}: {r.text[:200]}")
        return r.json()

    def calendar_list(self) -> list[dict]:
        out, page = [], None
        for _ in range(MAX_PAGES):
            d = self._get(f"{API}/users/me/calendarList", {"pageToken": page} if page else None)
            for c in d.get("items", []):
                out.append({"remote_id": c["id"], "name": c.get("summaryOverride") or c.get("summary") or "Calendar",
                            "can_edit": c.get("accessRole") in ("owner", "writer"), "hex_color": c.get("backgroundColor") or "",
                            "owner": c["id"] if c.get("primary") else ""})
            page = d.get("nextPageToken")
            if not page:
                break
        return out

    def entries(self, remote_id: str, lo: datetime, hi: datetime) -> list[dict]:
        out, page = [], None
        for _ in range(MAX_PAGES):
            params = {"timeMin": _iso(lo), "timeMax": _iso(hi), "singleEvents": "true", "maxResults": "2500"}
            if page:
                params["pageToken"] = page
            d = self._get(f"{API}/calendars/{quote(remote_id, safe='')}/events", params)
            out.extend(event_fields(ev) for ev in d.get("items", []) if ev.get("start"))
            page = d.get("nextPageToken")
            if not page:
                break
        return out
