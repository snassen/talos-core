"""iCloud calendars over CalDAV, read-only.

iCloud takes the same app-specific password as its mail (the Keychain item of the iCloud mail
account), so no new secret is needed. The calendar home is found from the principal; each calendar
that holds events (VEVENT) is listed with its colour and whether the owner may write to it. Its entries
for a window are asked for with a calendar-query that has the server expand recurring series
(<C:expand>), so each occurrence comes back on its own, in UTC, with its RECURRENCE-ID.

Only PROPFIND, REPORT and GET are sent: the sync guard allows those two WebDAV read methods by name.
Writing is talos.calwrite's, for an entry the owner saves in Talos Web.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
import icalendar

ROOT = "https://caldav.icloud.com/"
NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav", "a": "http://apple.com/ns/ical/",
      "cs": "http://calendarserver.org/ns/"}
_PROPFIND = ('<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"'
             ' xmlns:a="http://apple.com/ns/ical/" xmlns:cs="http://calendarserver.org/ns/"><d:prop>{}</d:prop></d:propfind>')
_QUERY = ('<?xml version="1.0" encoding="utf-8"?><c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
          '<d:prop><d:getetag/><c:calendar-data><c:expand start="{lo}" end="{hi}"/></c:calendar-data></d:prop>'
          '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"><c:time-range start="{lo}" end="{hi}"/>'
          '</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>')


class CalDAVError(RuntimeError):
    pass


def _stamp(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _day_utc(d: date) -> datetime:
    return datetime.combine(d, time(0), tzinfo=timezone.utc)


def _instant(value, zone: ZoneInfo) -> tuple[datetime, bool]:
    """(an instant, all day). A date is all day, at midnight UTC; a floating time is read in the owner's zone."""
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=zone)), False
    return _day_utc(value), True


def _minutes_before(trigger) -> int | None:
    td = getattr(trigger, "dt", None)
    if isinstance(td, timedelta) and td <= timedelta(0):
        return int(-td.total_seconds() // 60)
    return None


def event_fields(ev: icalendar.Event, href: str, etag: str | None, *, recurring_series: bool, zone: ZoneInfo) -> dict:
    """One VEVENT (an occurrence when the server expanded it) as calendar_entry's columns."""
    start, all_day = _instant(ev.decoded("DTSTART"), zone)
    if ev.get("DTEND") is not None:
        end, _ = _instant(ev.decoded("DTEND"), zone)
    elif ev.get("DURATION") is not None:
        end = start + ev.decoded("DURATION")
    else:
        end = start + (timedelta(days=1) if all_day else timedelta(0))
    rid = ev.get("RECURRENCE-ID")
    rid_text = rid.to_ical().decode() if rid is not None else None
    attendees = []
    for a in (ev.get("ATTENDEE") or []) if isinstance(ev.get("ATTENDEE"), list) else ([ev["ATTENDEE"]] if ev.get("ATTENDEE") else []):
        attendees.append({"name": str(a.params.get("CN", "")), "address": str(a).removeprefix("mailto:").removeprefix("MAILTO:"),
                          "type": str(a.params.get("ROLE", "")).lower(), "response": str(a.params.get("PARTSTAT", "")).lower()})
    org = ev.get("ORGANIZER")
    alarms = [_minutes_before(al.get("TRIGGER")) for al in ev.walk("VALARM")]
    reminder = next((m for m in alarms if m is not None), None)
    attrs = {"organizer": {"name": str(org.params.get("CN", "")) if org is not None else "",
                           "address": str(org).removeprefix("mailto:").removeprefix("MAILTO:") if org is not None else ""},
             "attendees": attendees[:100], "attendee_count": len(attendees),
             "cancelled": str(ev.get("STATUS", "")).upper() == "CANCELLED",
             "recurring": bool(rid is not None or ev.get("RRULE") is not None or recurring_series),
             "href": href, "etag": etag, "uid": str(ev.get("UID", "")), "recurrence_id": rid_text,
             "web_link": "", "join_url": "", "response": ""}
    return {"remote_id": href + (f"#{rid_text}" if rid_text else ""), "title": str(ev.get("SUMMARY", "")).strip() or "(no title)",
            "starts_at": start, "ends_at": max(end, start), "all_day": all_day,
            "location": str(ev.get("LOCATION", "")).strip(), "body": str(ev.get("DESCRIPTION", ""))[:20000],
            "show_as": "free" if str(ev.get("TRANSP", "")).upper() == "TRANSPARENT" else "busy",
            "reminder_minutes": reminder, "attrs": attrs}


def parse_calendar_data(text: str, href: str, etag: str | None, zone: ZoneInfo) -> list[dict]:
    cal = icalendar.Calendar.from_ical(text)
    series = cal.get("X-MASTER-RRULE") is not None
    return [event_fields(ev, href, etag, recurring_series=series, zone=zone) for ev in cal.walk("VEVENT")]


class ICloudCalendarSource:
    def __init__(self, username: str, password: Callable[[], str], http: httpx.Client | None = None, *,
                 zone: ZoneInfo | None = None, root: str = ROOT):
        self.username = username
        self.password = password
        self.http = http
        self.zone = zone or ZoneInfo("Europe/Stockholm")
        self.root = root
        self._home: str | None = None

    def _client(self) -> httpx.Client:
        if self.http is None:
            self.http = httpx.Client(timeout=60, follow_redirects=True)
        return self.http

    def _dav(self, method: str, url: str, body: str, depth: str) -> ET.Element:
        if method not in ("PROPFIND", "REPORT"):
            raise CalDAVError(f"{method} is not a read method")
        r = self._client().request("PROPFIND" if method == "PROPFIND" else "REPORT", url, content=body.encode(),
                                   headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
                                   auth=(self.username, self.password()))
        if r.status_code != 207:
            raise CalDAVError(f"{method} {url} -> {r.status_code}: {r.text[:200]}")
        return ET.fromstring(r.content)

    def home(self) -> str:
        if self._home is None:
            x = self._dav("PROPFIND", self.root, _PROPFIND.format("<d:current-user-principal/>"), "0")
            principal = urljoin(self.root, x.findtext(".//d:current-user-principal/d:href", namespaces=NS) or "")
            x = self._dav("PROPFIND", principal, _PROPFIND.format("<c:calendar-home-set/>"), "0")
            self._home = urljoin(principal, x.findtext(".//c:calendar-home-set/d:href", namespaces=NS) or "")
            if not self._home:
                raise CalDAVError("iCloud gave no calendar home")
        return self._home

    def calendar_list(self) -> list[dict]:
        """The calendars that hold events: {remote_id (the calendar's URL), name, can_edit, hex_color}."""
        home = self.home()
        x = self._dav("PROPFIND", home, _PROPFIND.format(
            "<d:displayname/><d:resourcetype/><a:calendar-color/><c:supported-calendar-component-set/>"
            "<d:current-user-privilege-set/><cs:source/>"), "1")
        out = []
        for resp in x.findall("d:response", NS):
            feed = resp.findtext(".//cs:source/d:href", namespaces=NS)
            if resp.find(".//d:resourcetype/cs:subscribed", NS) is not None and feed:
                # A subscription: iCloud keeps only the feed's address; the events are read from it.
                url = "https://" + feed.split("://", 1)[1] if feed.startswith(("webcal://", "http://", "https://")) else None
                if url:
                    out.append({"remote_id": url, "name": (resp.findtext(".//d:displayname", namespaces=NS) or "Calendar").strip(),
                                "can_edit": False, "hex_color": (resp.findtext(".//a:calendar-color", namespaces=NS) or "")[:7],
                                "owner": "", "subscribed": True})
                continue
            if resp.find(".//d:resourcetype/c:calendar", NS) is None:
                continue
            comps = [c.get("name") for c in resp.findall(".//c:supported-calendar-component-set/c:comp", NS)]
            if comps and "VEVENT" not in comps:
                continue  # a reminders or to-do list
            privs = {p.tag.split("}")[1] for p in resp.findall(".//d:current-user-privilege-set/d:privilege/*", NS)}
            color = (resp.findtext(".//a:calendar-color", namespaces=NS) or "")[:7]
            out.append({"remote_id": urljoin(home, resp.findtext("d:href", namespaces=NS)),
                        "name": (resp.findtext(".//d:displayname", namespaces=NS) or "Calendar").strip(),
                        "can_edit": bool(privs & {"write", "write-content", "all"}), "hex_color": color, "owner": self.username})
        return out

    def entries(self, remote_id: str, lo: datetime, hi: datetime) -> list[dict]:
        if not remote_id.startswith(self.root.split("//")[0] + "//") or "icloud.com" not in remote_id:
            return self._feed(remote_id, lo, hi)
        x = self._dav("REPORT", remote_id, _QUERY.format(lo=_stamp(lo), hi=_stamp(hi)), "1")
        out = []
        for resp in x.findall("d:response", NS):
            data = resp.findtext(".//c:calendar-data", namespaces=NS)
            if not data:
                continue
            href = urljoin(remote_id, resp.findtext("d:href", namespaces=NS))
            out.extend(parse_calendar_data(data, href, resp.findtext(".//d:getetag", namespaces=NS), self.zone))
        return out

    def _feed(self, url: str, lo: datetime, hi: datetime) -> list[dict]:
        """A subscribed calendar's feed (a plain GET, no password sent), its series expanded here."""
        import recurring_ical_events
        r = self._client().get(url)
        if r.status_code != 200:
            raise CalDAVError(f"GET {url.split('?')[0]} -> {r.status_code}")
        cal = icalendar.Calendar.from_ical(r.content)
        out = []
        for ev in recurring_ical_events.of(cal).between(lo, hi):
            f = event_fields(ev, url, None, recurring_series=False, zone=self.zone)
            f["remote_id"] = f"{url}#{f['attrs']['uid']}#{f['starts_at'].isoformat()}"
            f["attrs"]["recurring"] = True
            out.append(f)
        return out
