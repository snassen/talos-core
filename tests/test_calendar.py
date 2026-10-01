"""The calendar: Talos's own calendars and entries, the read-only Microsoft 365 copy, and its API."""

from datetime import date, datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from talos import calendars, work
from talos.calendars import CalendarError
from talos.config import Settings
from talos.sources.m365calendar import entry_fields
from talos.web import app

H = {"X-Talos": "1"}
TODAY = date(2026, 9, 28)


def client(database, vault, **kw):
    return TestClient(app.create(Settings(home=vault.root.parent, dsn=database), allowed_hosts=["testserver"], **kw))


def _event(eid, subject, start, end, *, all_day=False, cancelled=False):
    return {"id": eid, "subject": subject, "isAllDay": all_day, "isCancelled": cancelled, "showAs": "busy",
            "start": {"dateTime": start + ".0000000", "timeZone": "UTC"}, "end": {"dateTime": end + ".0000000", "timeZone": "UTC"},
            "location": {"displayName": "Room 4"}, "organizer": {"emailAddress": {"name": "Anna", "address": "anna@example.com"}},
            "attendees": [{"type": "required", "emailAddress": {"name": "Bo", "address": "bo@example.com"},
                           "status": {"response": "accepted"}}],
            "webLink": "https://outlook.office365.com/owa/?itemid=x", "bodyPreview": "Agenda"}


class FakeCalendars:
    """A Graph calendar source that answers from memory; a calendar in fail raises."""

    def __init__(self, cals, events, fail=()):
        self.cals, self.evs, self.fail = cals, events, set(fail)

    def calendars(self):
        return self.cals

    def events(self, calendar_id, start, end):
        if calendar_id in self.fail:
            raise RuntimeError("403 Forbidden")
        return self.evs.get(calendar_id, [])


def _talos(conn):
    return next(c for c in calendars.list_calendars(conn) if c["source"] == "talos")


def test_there_is_always_a_talos_calendar_and_it_is_the_default_for_new_entries(conn):
    cals = calendars.list_calendars(conn)
    assert [(c["name"], c["source"], c["is_default"], c["read_only"]) for c in cals] == [("Talos", "talos", True, False)]
    e = calendars.create_entry(conn, title="Dentist", start="2026-09-29T08:00:00+02:00", end="2026-09-29T09:00:00+02:00")
    assert e["calendar_id"] == cals[0]["id"]


def test_an_entry_is_created_and_then_edited(conn):
    cal = _talos(conn)
    e = calendars.create_entry(conn, calendar_id=cal["id"], title=" Planning ", start="2026-09-29T08:00:00+02:00",
                               end="2026-09-29T09:00:00+02:00", location="Studio room")
    assert (e["title"], e["location"], e["all_day"], e["read_only"]) == ("Planning", "Studio room", False, False)
    e2 = calendars.update_entry(conn, e["id"], title="Planning, week 40", end="2026-09-29T09:30:00+02:00")
    assert e2["title"] == "Planning, week 40"
    assert datetime.fromisoformat(e2["end"]) == datetime(2026, 9, 29, 7, 30, tzinfo=timezone.utc)
    assert datetime.fromisoformat(e2["start"]) == datetime(2026, 9, 29, 6, 0, tzinfo=timezone.utc)


def test_an_all_day_entry_keeps_its_dates_with_the_last_day_inclusive(conn):
    e = calendars.create_entry(conn, title="Conference", all_day=True, start="2026-10-05", end="2026-10-07")
    assert (e["start_date"], e["end_date"]) == ("2026-10-05", "2026-10-07")
    assert [x["id"] for x in calendars.entries(conn, date(2026, 10, 7), date(2026, 10, 7))] == [e["id"]]
    assert calendars.entries(conn, date(2026, 10, 8), date(2026, 10, 9)) == []
    one = calendars.update_entry(conn, e["id"], all_day=False, start="2026-10-05T09:00:00+02:00", end="2026-10-05T10:00:00+02:00")
    assert not one["all_day"] and "start_date" not in one


def test_the_end_before_the_start_or_a_missing_title_is_refused(conn):
    with pytest.raises(CalendarError, match="before the start"):
        calendars.create_entry(conn, title="x", start="2026-09-29T09:00:00+02:00", end="2026-09-29T08:00:00+02:00")
    with pytest.raises(CalendarError, match="title"):
        calendars.create_entry(conn, title="  ", start="2026-09-29T09:00:00+02:00", end="2026-09-29T10:00:00+02:00")
    with pytest.raises(CalendarError, match="time zone"):
        calendars.create_entry(conn, title="x", start="2026-09-29T09:00:00", end="2026-09-29T10:00:00")


def test_entries_are_listed_for_the_days_asked_for_and_from_the_calendars_asked_for(conn):
    cal = _talos(conn)
    other = calendars.create_calendar(conn, "Personal")
    a = calendars.create_entry(conn, calendar_id=cal["id"], title="Mon", start="2026-09-28T10:00:00+00:00", end="2026-09-28T11:00:00+00:00")
    b = calendars.create_entry(conn, calendar_id=other["id"], title="Mon too", start="2026-09-28T12:00:00+00:00", end="2026-09-28T13:00:00+00:00")
    calendars.create_entry(conn, calendar_id=cal["id"], title="Far away", start="2026-12-01T10:00:00+00:00", end="2026-12-01T11:00:00+00:00")
    assert [e["id"] for e in calendars.entries(conn, TODAY, TODAY)] == [a["id"], b["id"]]
    assert [e["id"] for e in calendars.entries(conn, TODAY, TODAY, calendar_ids=[other["id"]])] == [b["id"]]
    assert other["color"] != cal["color"]


def test_removing_an_entry_puts_it_in_the_trash_and_it_can_come_back(conn):
    e = calendars.create_entry(conn, title="Maybe", start="2026-09-28T10:00:00+00:00", end="2026-09-28T11:00:00+00:00")
    calendars.remove_entry(conn, e["id"])
    assert calendars.entries(conn, TODAY, TODAY) == []
    assert conn.execute("select removed_at is not null as r from calendar_entry where id = %s", (e["id"],)).fetchone()["r"]
    calendars.remove_entry(conn, e["id"], removed=False)
    assert [x["id"] for x in calendars.entries(conn, TODAY, TODAY)] == [e["id"]]


def test_one_calendar_is_the_default_and_it_is_always_a_talos_one(conn):
    first = _talos(conn)
    second = calendars.create_calendar(conn, "Focus time")
    calendars.update_calendar(conn, second["id"], is_default=True)
    defaults = [c["name"] for c in calendars.list_calendars(conn) if c["is_default"]]
    assert defaults == ["Focus time"]
    calendars.sync_m365(conn, "work", FakeCalendars([{"id": "K", "name": "Kalender", "canEdit": True},
                                                   {"id": "H", "name": "Helgdagar", "canEdit": False}], {}), today=TODAY)
    k, hol = (next(c for c in calendars.list_calendars(conn) if c["name"] == n) for n in ("Kalender", "Helgdagar"))
    with pytest.raises(CalendarError, match="cannot be written"):
        calendars.update_calendar(conn, hol["id"], is_default=True)
    assert calendars.update_calendar(conn, k["id"], is_default=True)["is_default"]  # a real calendar the owner can write
    assert calendars.update_calendar(conn, first["id"], is_default=True)["is_default"]


def test_the_m365_copy_is_read_only(conn):
    calendars.sync_m365(conn, "work", FakeCalendars([{"id": "K", "name": "Kalender"}],
                                                   {"K": [_event("e1", "Steering group", "2026-09-28T08:00:00", "2026-09-28T09:00:00")]}), today=TODAY)
    [e] = calendars.entries(conn, TODAY, TODAY)
    assert (e["title"], e["read_only"], e["location"], e["organizer"]["name"], e["attendee_count"]) == (
        "Steering group", True, "Room 4", "Anna", 1)
    with pytest.raises(CalendarError, match="read-only"):
        calendars.update_entry(conn, e["id"], title="Mine now")
    with pytest.raises(CalendarError, match="read-only"):
        calendars.remove_entry(conn, e["id"])
    with pytest.raises(CalendarError, match="read-only"):
        calendars.create_entry(conn, calendar_id=e["calendar_id"], title="x", start="2026-09-28T10:00:00+00:00",
                               end="2026-09-28T11:00:00+00:00")


def test_the_m365_copy_keeps_the_owners_colour_and_visibility_and_marks_what_went_away(conn):
    src = FakeCalendars([{"id": "K", "name": "Kalender"}, {"id": "H", "name": "Helgdagar"}],
                        {"K": [_event("e1", "Standup", "2026-09-28T07:00:00", "2026-09-28T07:15:00"),
                               _event("e2", "Retro", "2026-09-29T13:00:00", "2026-09-29T14:00:00")],
                         "H": [_event("h1", "Holiday", "2026-10-01T00:00:00", "2026-10-02T00:00:00", all_day=True)]})
    st = calendars.sync_m365(conn, "work", src, today=TODAY)
    assert (st["calendars"], st["entries"], st["gone"]) == (2, 3, 0)
    k = next(c for c in calendars.list_calendars(conn) if c["name"] == "Kalender")
    calendars.update_calendar(conn, k["id"], color=8, visible=False)
    src.evs["K"] = [_event("e2", "Retro (moved)", "2026-09-29T14:00:00", "2026-09-29T15:00:00")]
    src.cals = [{"id": "K", "name": "Kalender"}]
    st = calendars.sync_m365(conn, "work", src, today=TODAY)
    assert st["gone"] == 1
    cals = [c for c in calendars.list_calendars(conn) if c["source"] == "m365"]
    assert [(c["name"], c["color"], c["visible"]) for c in cals] == [("Kalender", 8, False)]
    assert [e["title"] for e in calendars.entries(conn, TODAY, TODAY + timedelta(days=5))] == ["Retro (moved)"]
    assert calendars.last_sync(conn)["entries"] == 1


def test_an_all_day_m365_entry_lands_on_its_own_date(conn):
    f = entry_fields(_event("h1", "Holiday", "2026-10-01T00:00:00", "2026-10-02T00:00:00", all_day=True))
    assert f["all_day"] and f["starts_at"] == datetime(2026, 10, 1, tzinfo=timezone.utc)
    calendars.sync_m365(conn, "work", FakeCalendars([{"id": "H", "name": "Helgdagar"}],
                                                   {"H": [_event("h1", "Holiday", "2026-10-01T00:00:00", "2026-10-02T00:00:00", all_day=True)]}), today=TODAY)
    [e] = calendars.entries(conn, date(2026, 10, 1), date(2026, 10, 1))
    assert (e["start_date"], e["end_date"]) == ("2026-10-01", "2026-10-01")
    assert calendars.entries(conn, date(2026, 10, 2), date(2026, 10, 3)) == []


def test_a_calendar_that_fails_does_not_stop_the_others_nor_lose_its_entries(conn):
    src = FakeCalendars([{"id": "K", "name": "Kalender"}, {"id": "S", "name": "Shared"}],
                        {"K": [_event("e1", "Standup", "2026-09-28T07:00:00", "2026-09-28T07:15:00")],
                         "S": [_event("s1", "Board", "2026-09-28T09:00:00", "2026-09-28T10:00:00")]})
    calendars.sync_m365(conn, "work", src, today=TODAY)
    src.fail = {"S"}
    st = calendars.sync_m365(conn, "work", src, today=TODAY)
    assert st["failed"] == ["Shared"]
    assert sorted(e["title"] for e in calendars.entries(conn, TODAY, TODAY)) == ["Board", "Standup"]


def test_an_entry_can_be_split_off_from_a_work_item(conn):
    wid = work.create(conn, "Migrate the VPN")
    e = calendars.create_entry(conn, title="VPN cut-over", start="2026-10-10T18:00:00+02:00", end="2026-10-10T22:00:00+02:00",
                               work_item_id=wid)
    assert e["work_item_id"] == wid


def test_the_api_shows_creates_edits_and_removes_entries(conn, vault, database):
    c = client(database, vault)
    d = c.get("/api/calendar?start=2026-09-28&end=2026-10-04").json()
    cal = d["calendars"][0]
    assert (cal["name"], cal["is_default"], d["entries"], d["synced"]) == ("Talos", True, [], None)
    assert c.post("/api/calendar/entries", json={"title": "x"}).status_code == 403  # no X-Talos header
    r = c.post("/api/calendar/entries", json={"title": "Review", "start": "2026-09-30T10:00:00+02:00",
                                              "end": "2026-09-30T11:00:00+02:00"}, headers=H)
    assert r.status_code == 200, r.text
    eid = r.json()["id"]
    r = c.post(f"/api/calendar/entries/{eid}", json={"title": "Review, again", "location": "Online"}, headers=H)
    assert (r.json()["title"], r.json()["location"]) == ("Review, again", "Online")
    bad = c.post(f"/api/calendar/entries/{eid}", json={"end": "2026-09-30T09:00:00+02:00"}, headers=H)
    assert bad.status_code == 400 and "before the start" in bad.json()["error"]
    assert [e["title"] for e in c.get("/api/calendar?start=2026-09-28&end=2026-10-04").json()["entries"]] == ["Review, again"]
    c.post(f"/api/calendar/entries/{eid}", json={"removed": True}, headers=H)
    assert c.get("/api/calendar?start=2026-09-28&end=2026-10-04").json()["entries"] == []


def test_the_api_makes_a_calendar_recolours_hides_and_makes_it_the_default(conn, vault, database):
    c = client(database, vault)
    r = c.post("/api/calendar/calendars", json={"name": "Personal"}, headers=H).json()
    r = c.post(f"/api/calendar/calendars/{r['id']}", json={"color": 5, "visible": False, "is_default": True}, headers=H).json()
    assert (r["color"], r["visible"], r["is_default"]) == (5, False, True)
    assert c.post(f"/api/calendar/calendars/{r['id']}", json={"color": 9}, headers=H).status_code == 400


def test_refresh_copies_the_m365_calendars_through_the_injected_source(conn, vault, database):
    src = FakeCalendars([{"id": "K", "name": "Kalender"}],
                        {"K": [_event("e1", "Standup", date.today().isoformat() + "T07:00:00", date.today().isoformat() + "T07:15:00")]})
    c = client(database, vault, calendar_source=lambda acct: src)
    r = c.post("/api/calendar/sync", json={}, headers=H).json()
    assert r["accounts"]["work"]["entries"] == 1
    d = c.get(f"/api/calendar?start={date.today()}&end={date.today()}").json()
    assert [e["title"] for e in d["entries"]] == ["Standup"]
    assert d["synced"]["account_id"] == "work"
