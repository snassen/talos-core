"""Writing to the real calendars (talos.calwrite) and reading iCloud and Google (the sources), against fakes."""

import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import icalendar
import pytest
from psycopg.types.json import Jsonb
from starlette.testclient import TestClient

from talos import calendars, calwrite, work
from talos.calendars import CalendarError
from talos.config import Settings
from talos.sources.googlecalendar import GoogleCalendarSource
from talos.sources.icloudcalendar import ICloudCalendarSource, parse_calendar_data
from talos.web import app

H = {"X-Talos": "1"}
Z = ZoneInfo("Europe/Stockholm")
READY = {"talos": (True, None), "m365": (True, None), "icloud": (True, None), "google": (True, None)}


def _graph_event(eid, subject, start, end, **kw):
    return {"id": eid, "subject": subject, "isAllDay": kw.get("all_day", False), "showAs": "busy", "type": kw.get("type", "singleInstance"),
            "start": {"dateTime": start, "timeZone": "UTC"}, "end": {"dateTime": end, "timeZone": "UTC"},
            "location": {"displayName": kw.get("location", "")}, "bodyPreview": kw.get("body", ""),
            "attendees": kw.get("attendees", []), "isOrganizer": kw.get("is_organizer", True),
            "isReminderOn": kw.get("reminder") is not None, "reminderMinutesBeforeStart": kw.get("reminder") or 0,
            "organizer": {"emailAddress": {"name": "Alex", "address": "owner@company.example"}}}


class FakeOutlook:
    """Graph's calendar endpoints over a few events; records every request."""

    def __init__(self):
        self.events, self.calls, self.n = {}, [], 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, req.url.path, body))
        assert "attendees" not in (body or {}), "Talos must never name an attendee"
        assert "/send" not in req.url.path and "cancel" not in req.url.path
        parts = req.url.path.split("/")
        if req.method == "POST" and parts[-1] == "events":
            self.n += 1
            eid = f"E{self.n}"
            self.events[eid] = _graph_event(eid, body["subject"], body["start"]["dateTime"], body["end"]["dateTime"],
                                            all_day=body.get("isAllDay"), location=(body.get("location") or {}).get("displayName", ""),
                                            body=(body.get("body") or {}).get("content", ""),
                                            reminder=body.get("reminderMinutesBeforeStart") if body.get("isReminderOn") else None)
            return httpx.Response(201, json=self.events[eid])
        eid = parts[-1]
        if req.method == "PATCH":
            ev = self.events[eid]
            if "subject" in body:
                ev["subject"] = body["subject"]
            for k in ("start", "end"):
                if k in body:
                    ev[k] = body[k]
            if "isReminderOn" in body:
                ev["isReminderOn"], ev["reminderMinutesBeforeStart"] = body["isReminderOn"], body["reminderMinutesBeforeStart"]
            return httpx.Response(200, json=ev)
        if req.method == "DELETE":
            self.events.pop(eid)
            return httpx.Response(204)
        raise AssertionError(f"unexpected {req.method} {req.url}")


def _m365_calendar(conn, **attrs):
    fake = type("S", (), {"calendars": lambda self: [{"id": "CAL", "name": "Kalender", "canEdit": attrs.get("can_edit", True)}],
                          "events": lambda self, cid, lo, hi: attrs.get("events", [])})()
    calendars.sync_m365(conn, "work", fake, today=date.today())
    return next(c for c in calendars.list_calendars(conn) if c["source"] == "m365")


def _writers(outlook=None, google=None, icloud=None):
    out = {}
    if outlook:
        out["m365"] = calwrite.M365Writer(lambda: "tok", httpx.Client(transport=httpx.MockTransport(outlook)))
    if google:
        out["google"] = calwrite.GoogleWriter(lambda: "tok", httpx.Client(transport=httpx.MockTransport(google)))
    if icloud:
        out["icloud"] = calwrite.ICloudWriter("me@icloud.com", lambda: "pw", httpx.Client(transport=httpx.MockTransport(icloud)), zone=Z)
    return out


def test_a_new_outlook_entry_is_written_to_outlook_first_and_the_answer_is_the_copy(conn):
    cal = _m365_calendar(conn)
    fake = FakeOutlook()
    wid = work.create(conn, "Firewall change")
    e = calwrite.create_entry(conn, writers=_writers(fake), calendar_id=cal["id"], title="Change window",
                              start="2026-10-10T18:00:00+02:00", end="2026-10-10T20:00:00+02:00", location="Server room",
                              reminder_minutes=15, work_item_id=wid)
    method, path, body = fake.calls[0]
    assert (method, path) == ("POST", "/v1.0/me/calendars/CAL/events")
    assert body["start"] == {"dateTime": "2026-10-10T16:00:00", "timeZone": "UTC"} and body["isReminderOn"] and body["reminderMinutesBeforeStart"] == 15
    assert (e["title"], e["location"], e["reminder_minutes"], e["work_item_id"], e["source"]) == ("Change window", "Server room", 15, wid, "m365")


def test_an_outlook_edit_sends_only_what_changed(conn):
    cal = _m365_calendar(conn)
    fake = FakeOutlook()
    w = _writers(fake)
    e = calwrite.create_entry(conn, writers=w, calendar_id=cal["id"], title="Review", start="2026-10-10T08:00:00+00:00",
                              end="2026-10-10T09:00:00+00:00")
    e2 = calwrite.update_entry(conn, e["id"], writers=w, title="Review, again", reminder_minutes=10, location="")
    assert fake.calls[-1][0] == "PATCH" and set(fake.calls[-1][2]) == {"subject", "isReminderOn", "reminderMinutesBeforeStart"}
    assert (e2["title"], e2["reminder_minutes"]) == ("Review, again", 10)
    n = len(fake.calls)
    assert calwrite.update_entry(conn, e["id"], writers=w, title="Review, again")["title"] == "Review, again"
    assert len(fake.calls) == n  # nothing changed, nothing sent


def test_an_entry_with_invitees_a_series_or_someone_elses_meeting_is_not_written(conn):
    guest = [{"type": "required", "emailAddress": {"name": "Anna", "address": "anna@example.com"}, "status": {"response": "none"}}]
    cal = _m365_calendar(conn, events=[
        _graph_event("M1", "Board", "2026-10-01T08:00:00", "2026-10-01T09:00:00", attendees=guest),
        _graph_event("M2", "Standup", "2026-10-02T08:00:00", "2026-10-02T08:15:00", type="occurrence"),
        _graph_event("M3", "Their call", "2026-10-03T08:00:00", "2026-10-03T09:00:00", is_organizer=False)])
    fake = FakeOutlook()
    rows = {r["title"]: r for r in calendars.entries(conn, date(2026, 10, 1), date(2026, 10, 3))}
    for title, why in (("Board", "invitees"), ("Standup", "repeating series"), ("Their call", "someone else's meeting")):
        with pytest.raises(calwrite.WriteRefused, match=why):
            calwrite.update_entry(conn, rows[title]["id"], writers=_writers(fake), title="x")
        with pytest.raises(calwrite.WriteRefused, match=why):
            calwrite.remove_entry(conn, rows[title]["id"], writers=_writers(fake))
    assert fake.calls == []
    cals = calendars.list_calendars(conn)
    found = calendars.entries(conn, date(2026, 10, 1), date(2026, 10, 3))
    calwrite.annotate(conn, cals, found, READY)
    assert {e["title"]: e["read_only"] for e in found} == {"Board": True, "Standup": True, "Their call": True}
    assert next(c for c in cals if c["id"] == cal["id"])["writable"]


def test_notes_cut_short_by_outlook_are_not_overwritten(conn):
    _m365_calendar(conn, events=[_graph_event("M1", "Long notes", "2026-10-01T08:00:00", "2026-10-01T09:00:00", body="x" * 255)])
    [e] = calendars.entries(conn, date(2026, 10, 1), date(2026, 10, 1))
    with pytest.raises(calwrite.WriteRefused, match="notes are longer"):
        calwrite.update_entry(conn, e["id"], writers=_writers(FakeOutlook()), body="shorter")


def test_removing_an_outlook_entry_sends_it_to_deleted_items_and_cannot_be_undone_from_here(conn):
    cal = _m365_calendar(conn)
    fake = FakeOutlook()
    w = _writers(fake)
    e = calwrite.create_entry(conn, writers=w, calendar_id=cal["id"], title="Drop me", start="2026-10-10T08:00:00+00:00",
                              end="2026-10-10T09:00:00+00:00")
    calwrite.remove_entry(conn, e["id"], writers=w)
    assert fake.calls[-1][:2] == ("DELETE", "/v1.0/me/events/E1")
    assert calendars.entries(conn, date(2026, 10, 10), date(2026, 10, 10)) == []
    with pytest.raises(CalendarError):
        calwrite.remove_entry(conn, e["id"], removed=False, writers=w)


def test_a_calendar_that_cannot_be_written_or_is_not_ready_says_why(conn):
    cal = _m365_calendar(conn, can_edit=False)
    cals = calendars.list_calendars(conn)
    calwrite.annotate(conn, cals, [], READY)
    assert next(c for c in cals if c["id"] == cal["id"])["write_note"] == "this calendar is read-only"
    with pytest.raises(calwrite.WriteRefused, match="read-only"):
        calwrite.create_entry(conn, writers=_writers(FakeOutlook()), calendar_id=cal["id"], title="x",
                              start="2026-10-10T08:00:00+00:00", end="2026-10-10T09:00:00+00:00")
    cals = calendars.list_calendars(conn)
    calwrite.annotate(conn, cals, [], {**READY, "m365": (False, "writing needs the Calendars.ReadWrite permission")})
    assert not next(c for c in cals if c["id"] == cal["id"])["writable"]


# ---------------------------------------------------------------- Google

class FakeGoogle:
    def __init__(self):
        self.calls, self.events, self.n = [], {}, 0

    def __call__(self, req):
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, req.url.path, dict(req.url.params), body))
        assert req.url.params.get("sendUpdates") == "none", "every Google write says sendUpdates=none"
        assert "attendees" not in (body or {})
        if req.method == "POST":
            self.n += 1
            ev = {"id": f"g{self.n}", "status": "confirmed", **body}
            self.events[ev["id"]] = ev
            return httpx.Response(200, json=ev)
        eid = req.url.path.split("/")[-1]
        if req.method == "PATCH":
            self.events[eid].update(body)
            return httpx.Response(200, json=self.events[eid])
        if req.method == "DELETE":
            self.events.pop(eid)
            return httpx.Response(204)
        raise AssertionError(req.method)


def _google_calendar(conn):
    src = type("G", (), {"calendar_list": lambda self: [{"remote_id": "primary@gmail.com", "name": "Alex", "can_edit": True,
                                                          "hex_color": "#039be5", "owner": ""}],
                         "entries": lambda self, r, lo, hi: []})()
    calendars.sync_source(conn, "google", "gmail", src)
    return next(c for c in calendars.list_calendars(conn) if c["source"] == "google")


def test_google_entries_are_written_with_sendupdates_none_and_a_popup_alert(conn):
    cal = _google_calendar(conn)
    fake = FakeGoogle()
    w = _writers(google=fake)
    e = calwrite.create_entry(conn, writers=w, calendar_id=cal["id"], title="Dentist", all_day=True, start="2026-10-12",
                              end="2026-10-12", reminder_minutes=60, show_as="free")
    body = fake.calls[0][3]
    assert body["start"] == {"date": "2026-10-12"} and body["end"] == {"date": "2026-10-13"}
    assert body["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 60}]} and body["transparency"] == "transparent"
    assert (e["start_date"], e["end_date"], e["reminder_minutes"], e["show_as"]) == ("2026-10-12", "2026-10-12", 60, "free")
    calwrite.update_entry(conn, e["id"], writers=w, title="Dentist, Östermalm")
    assert fake.calls[-1][0] == "PATCH" and fake.calls[-1][3] == {"summary": "Dentist, Östermalm"}
    calwrite.remove_entry(conn, e["id"], writers=w)
    assert fake.calls[-1][0] == "DELETE"


def test_the_google_source_reads_occurrences_all_day_dates_and_the_owners_own_answer():
    def api(req):
        if req.url.path.endswith("calendarList"):
            return httpx.Response(200, json={"items": [{"id": "me@gmail.com", "summary": "Alex", "accessRole": "owner", "primary": True},
                                                       {"id": "sv.swedish#holiday", "summary": "Helgdagar", "accessRole": "reader"}]})
        assert req.url.params["singleEvents"] == "true"
        return httpx.Response(200, json={"items": [
            {"id": "a", "summary": "Midsommar", "start": {"date": "2026-06-19"}, "end": {"date": "2026-06-20"}},
            {"id": "b_20261001", "summary": "Yoga", "recurringEventId": "b", "start": {"dateTime": "2026-10-01T18:00:00+02:00"},
             "end": {"dateTime": "2026-10-01T19:00:00+02:00"}, "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]},
             "attendees": [{"email": "me@gmail.com", "self": True, "responseStatus": "accepted"}]}]})
    src = GoogleCalendarSource(lambda: "tok", httpx.Client(transport=httpx.MockTransport(api)))
    assert [(c["name"], c["can_edit"]) for c in src.calendar_list()] == [("Alex", True), ("Helgdagar", False)]
    mid, yoga = src.entries("me@gmail.com", datetime(2026, 6, 1, tzinfo=timezone.utc), datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert mid["all_day"] and mid["starts_at"] == datetime(2026, 6, 19, tzinfo=timezone.utc)
    assert yoga["attrs"]["recurring"] and yoga["reminder_minutes"] == 30 and yoga["attrs"]["response"] == "accepted"


# ---------------------------------------------------------------- iCloud

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Apple Inc.//macOS//EN
BEGIN:VEVENT
UID:ABC
DTSTAMP:20260901T000000Z
DTSTART;TZID=Europe/Stockholm:20261005T090000
DTEND;TZID=Europe/Stockholm:20261005T100000
SUMMARY:Tandläkare
LOCATION:Östermalm
SEQUENCE:2
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT15M
END:VALARM
END:VEVENT
END:VCALENDAR
"""


class FakeICloud:
    def __init__(self):
        self.store = {"https://p1-caldav.icloud.com/1/calendars/home/ABC.ics": (ICS, '"e1"')}
        self.calls = []

    def __call__(self, req):
        self.calls.append((req.method, str(req.url), dict(req.headers)))
        url = str(req.url)
        if req.method == "GET":
            ics, etag = self.store[url]
            return httpx.Response(200, content=ics.encode(), headers={"ETag": etag})
        if req.method == "PUT":
            if req.headers.get("If-None-Match") == "*" and url in self.store:
                return httpx.Response(412)
            if req.headers.get("If-Match") and self.store.get(url, (None, None))[1] != req.headers["If-Match"]:
                return httpx.Response(412)
            n = int(self.store.get(url, ("", '"e0"'))[1].strip('"e')) + 1
            self.store[url] = (req.content.decode(), f'"e{n}"')
            return httpx.Response(201 if req.headers.get("If-None-Match") else 204)
        raise AssertionError(f"Talos sent {req.method} to iCloud")


def _icloud_calendar(conn):
    conn.execute("insert into account (id, provider, address, settings) values ('icloud', 'imap', 'me@icloud.com', %s)"
                 " on conflict do nothing", (Jsonb({"host": "imap.mail.me.com", "secret": "imap:me@icloud.com"}),))
    src = type("I", (), {"calendar_list": lambda self: [{"remote_id": "https://p1-caldav.icloud.com/1/calendars/home/", "name": "Egen",
                                                          "can_edit": True, "hex_color": "#CC73E1", "owner": ""}],
                         "entries": lambda self, r, lo, hi: parse_calendar_data(ICS, r + "ABC.ics", '"e1"', Z)})()
    calendars.sync_source(conn, "icloud", "icloud", src, today=date(2026, 10, 1))
    return next(c for c in calendars.list_calendars(conn) if c["source"] == "icloud")


def test_an_icloud_event_reads_its_zone_and_alert_and_an_edit_keeps_the_rest_of_it(conn):
    cal = _icloud_calendar(conn)
    [e] = calendars.entries(conn, date(2026, 10, 5), date(2026, 10, 5))
    assert datetime.fromisoformat(e["start"]) == datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc) and e["reminder_minutes"] == 15
    fake = FakeICloud()
    e2 = calwrite.update_entry(conn, e["id"], writers=_writers(icloud=fake), title="Tandläkare, kontroll")
    put = next(c for c in fake.calls if c[0] == "PUT")
    assert put[2]["if-match"] == '"e1"'  # never overwrites a change made on the phone meanwhile
    ev = icalendar.Calendar.from_ical(fake.store[put[1]][0]).walk("VEVENT")[0]
    assert (str(ev["SUMMARY"]), str(ev["LOCATION"]), int(ev["SEQUENCE"])) == ("Tandläkare, kontroll", "Östermalm", 3)
    assert len(list(ev.walk("VALARM"))) == 1 and e2["title"] == "Tandläkare, kontroll"
    with pytest.raises(calwrite.WriteRefused, match="no trash"):
        calwrite.remove_entry(conn, e["id"], writers=_writers(icloud=fake))
    assert cal["source"] == "icloud"


def test_a_change_made_elsewhere_meanwhile_is_not_overwritten_on_icloud(conn):
    _icloud_calendar(conn)
    [e] = calendars.entries(conn, date(2026, 10, 5), date(2026, 10, 5))
    fake = FakeICloud()
    fake.store["https://p1-caldav.icloud.com/1/calendars/home/ABC.ics"] = (ICS, '"e1"')
    real = fake.__call__

    def racing(req):  # the phone saves between Talos's read and its write
        if req.method == "PUT":
            fake.store[str(req.url)] = (ICS, '"e9"')
        return real(req)
    w = {"icloud": calwrite.ICloudWriter("me", lambda: "pw", httpx.Client(transport=httpx.MockTransport(racing)), zone=Z)}
    with pytest.raises(CalendarError, match="changed elsewhere"):
        calwrite.update_entry(conn, e["id"], writers=w, title="Mine")


def test_a_new_icloud_event_is_put_once_with_its_alert(conn):
    cal = _icloud_calendar(conn)
    fake = FakeICloud()
    e = calwrite.create_entry(conn, writers=_writers(icloud=fake), calendar_id=cal["id"], title="Padel",
                              start="2026-10-07T17:00:00+02:00", end="2026-10-07T18:30:00+02:00", reminder_minutes=30)
    put = next(c for c in fake.calls if c[0] == "PUT")
    assert put[2]["if-none-match"] == "*" and put[1].endswith(".ics")
    ev = icalendar.Calendar.from_ical(fake.store[put[1]][0]).walk("VEVENT")[0]
    assert ev.decoded("DTSTART") == datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc) and "ATTENDEE" not in ev
    assert (e["title"], e["reminder_minutes"]) == ("Padel", 30)


def test_the_icloud_source_lists_event_calendars_and_reads_expanded_occurrences():
    multi = """<?xml version="1.0"?><multistatus xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav" xmlns:CS="http://calendarserver.org/ns/">
<response><href>/1/calendars/home/</href><propstat><prop><displayname>Egen</displayname><resourcetype><collection/><C:calendar/></resourcetype>
<C:supported-calendar-component-set><C:comp name="VEVENT"/></C:supported-calendar-component-set>
<current-user-privilege-set><privilege><write/></privilege></current-user-privilege-set></prop></propstat></response>
<response><href>/1/calendars/todo/</href><propstat><prop><displayname>Handla</displayname><resourcetype><collection/><C:calendar/></resourcetype>
<C:supported-calendar-component-set><C:comp name="VTODO"/></C:supported-calendar-component-set></prop></propstat></response>
<response><href>/1/calendars/sub/</href><propstat><prop><displayname>Namnsdagar</displayname><resourcetype><collection/><CS:subscribed/></resourcetype>
<CS:source><href>webcal://example.com/names.ics</href></CS:source></prop></propstat></response>
</multistatus>"""
    occ = ("BEGIN:VCALENDAR\nVERSION:2.0\nX-MASTER-RRULE:FREQ=WEEKLY\nBEGIN:VEVENT\nUID:R\nDTSTAMP:20260901T000000Z\nDTSTART:20261001T160000Z\n"
           "DTEND:20261001T170000Z\nRECURRENCE-ID:20261001T160000Z\nSUMMARY:Kör\nEND:VEVENT\nEND:VCALENDAR\n")
    report = f"""<?xml version="1.0"?><multistatus xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><response><href>/1/calendars/home/R.ics</href>
<propstat><prop><getetag>"x"</getetag><C:calendar-data>{occ}</C:calendar-data></prop></propstat></response></multistatus>"""

    def dav(req):
        assert req.method in ("PROPFIND", "REPORT"), f"a read sent {req.method}"
        body = req.content.decode()
        if "current-user-principal" in body:
            return httpx.Response(207, text='<multistatus xmlns="DAV:"><response><href>/</href><propstat><prop><current-user-principal>'
                                            '<href>/1/principal/</href></current-user-principal></prop></propstat></response></multistatus>')
        if "calendar-home-set" in body:
            return httpx.Response(207, text='<multistatus xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><response><href>/1/principal/</href>'
                                            '<propstat><prop><C:calendar-home-set><href>/1/calendars/</href></C:calendar-home-set></prop>'
                                            '</propstat></response></multistatus>')
        if req.method == "PROPFIND":
            return httpx.Response(207, text=multi)
        return httpx.Response(207, text=report)
    src = ICloudCalendarSource("me", lambda: "pw", httpx.Client(transport=httpx.MockTransport(dav)), zone=Z, root="https://caldav.icloud.com/")
    cals = src.calendar_list()
    assert [(c["name"], c["can_edit"], c.get("subscribed", False)) for c in cals] == [("Egen", True, False), ("Namnsdagar", False, True)]
    assert cals[1]["remote_id"] == "https://example.com/names.ics"
    [e] = src.entries(cals[0]["remote_id"], datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert e["attrs"]["recurring"] and e["remote_id"].endswith("R.ics#20261001T160000Z") and e["title"] == "Kör"


def test_the_api_writes_through_the_injected_writers_and_shows_what_is_read_only(conn, vault, database):
    cal = _m365_calendar(conn)
    conn.commit()
    fake = FakeOutlook()
    c = TestClient(app.create(Settings(home=vault.root.parent, dsn=database), allowed_hosts=["testserver"],
                              calendar_writers=_writers(fake), calendar_readiness=READY))
    r = c.post("/api/calendar/entries", json={"calendar_id": cal["id"], "title": "Planning", "start": "2026-10-10T08:00:00+00:00",
                                              "end": "2026-10-10T09:00:00+00:00", "reminder_minutes": 15}, headers=H)
    assert r.status_code == 200, r.text
    assert (r.json()["read_only"], r.json()["removable"]) == (False, True)  # the answer says it, as the page's GET does
    d = c.get("/api/calendar?start=2026-10-10&end=2026-10-10").json()
    m365 = next(x for x in d["calendars"] if x["source"] == "m365")
    assert m365["writable"] and [e["read_only"] for e in d["entries"]] == [False]
    assert c.post(f"/api/calendar/calendars/{cal['id']}", json={"is_default": True}, headers=H).json()["is_default"]
