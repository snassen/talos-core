"""The timeline (Work › Timeline): work items as bars, binders spanning them, the calendar's load beside."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from starlette.testclient import TestClient

from talos import calendars, objects, timeline, work
from talos.config import Settings
from talos.web import app

H = {"X-Talos": "1"}
START, END = date(2026, 10, 1), date(2026, 10, 31)


def client(database, vault):
    return TestClient(app.create(Settings(home=vault.root.parent, dsn=database), allowed_hosts=["testserver"]))


def _binder(tl, name):
    return next(b for b in tl["binders"] if b["name"] == name)


def test_an_item_is_a_bar_a_milestone_open_ended_or_undated(conn):
    p = objects.create(conn, "project", "VPN Client Refresh")
    bar = work.create(conn, "Pilot group", home_id=p, status="doing", start_on="2026-10-05", due="2026-10-16")
    mile = work.create(conn, "Licence renewal", home_id=p, status="next", due="2026-10-20")
    run = work.create(conn, "Roll out", home_id=p, status="next", start_on="2026-10-19")
    work.create(conn, "Decide on the client", home_id=p)
    b = _binder(timeline.timeline(conn, START, END), "VPN Client Refresh")
    rows = {i["id"]: i for i in b["items"]}
    assert (rows[bar]["milestone"], rows[bar]["open_ended"]) == (False, False)
    assert (rows[mile]["milestone"], rows[run]["open_ended"]) == (True, True)
    assert [i["title"] for i in b["undated"]] == ["Decide on the client"]
    assert (b["span_start"], b["span_end"], b["derived"]) == (date(2026, 10, 5), date(2026, 10, 20), True)


def test_a_binder_with_its_own_dates_shows_them_even_without_items(conn):
    p = objects.create(conn, "project", "Network Gateway Replacement")
    objects.set_dates(conn, p, starts_on="2026-10-10", ends_on="2026-12-20")
    b = _binder(timeline.timeline(conn, START, END), "Network Gateway Replacement")
    assert (b["span_start"], b["span_end"], b["derived"], b["items"]) == (date(2026, 10, 10), date(2026, 12, 20), False, [])
    with pytest.raises(objects.ObjectError, match="after the end"):
        objects.set_dates(conn, p, starts_on="2026-12-21", ends_on="2026-12-20")


def test_a_start_after_the_due_date_is_refused(conn):
    with pytest.raises(work.WorkError, match="after the due date"):
        work.create(conn, "Backwards", start_on="2026-10-10", due="2026-10-01")
    wid = work.create(conn, "Forwards", due="2026-10-10")
    with pytest.raises(work.WorkError, match="after the due date"):
        work.update(conn, wid, start_on="2026-10-11")
    assert work.update(conn, wid, start_on="2026-10-02")["start_on"] == date(2026, 10, 2)


def test_filters_keep_to_a_kind_a_binder_and_its_children_and_leave_out_done(conn):
    area = objects.create(conn, "area", "Security")
    proj = objects.create(conn, "project", "FortiDLP Renewal")
    other = objects.create(conn, "project", "FinOps Product")
    objects.add(conn, area, [proj])
    work.create(conn, "Quote", home_id=proj, start_on="2026-10-01", due="2026-10-05")
    work.create(conn, "Shipped", home_id=proj, status="done", start_on="2026-10-01", due="2026-10-03")
    work.create(conn, "Pricing", home_id=other, start_on="2026-10-02", due="2026-10-09")
    names = lambda tl: sorted(b["name"] for b in tl["binders"])  # noqa: E731
    assert names(timeline.timeline(conn, START, END, binder=area)) == ["FortiDLP Renewal"]
    assert names(timeline.timeline(conn, START, END, kind="project")) == ["FinOps Product", "FortiDLP Renewal"]
    assert [i["title"] for i in _binder(timeline.timeline(conn, START, END), "FortiDLP Renewal")["items"]] == ["Quote"]
    assert len(_binder(timeline.timeline(conn, START, END, done=True), "FortiDLP Renewal")["items"]) == 2


def test_items_outside_the_window_are_counted_but_not_placed(conn):
    p = objects.create(conn, "project", "AWS Production Readiness")
    work.create(conn, "Later", home_id=p, start_on="2027-01-10", due="2027-01-20")
    work.create(conn, "Now", home_id=p, start_on="2026-10-10", due="2026-10-20")
    b = _binder(timeline.timeline(conn, START, END), "AWS Production Readiness")
    assert ([i["title"] for i in b["items"]], b["outside"]) == (["Now"], 1)


def test_busy_hours_count_a_double_booking_once():
    z = ZoneInfo("Europe/Stockholm")
    t = lambda h, m=0: datetime(2026, 10, 1, h, m, tzinfo=z)  # noqa: E731
    rows = [{"starts_at": t(9), "ends_at": t(10)}, {"starts_at": t(9, 30), "ends_at": t(11)}, {"starts_at": t(13), "ends_at": t(14)}]
    assert timeline.busy_hours(rows, [date(2026, 10, 1), date(2026, 10, 2)], z) == {date(2026, 10, 1): 3.0, date(2026, 10, 2): 0.0}


def test_the_load_counts_meeting_hours_running_items_and_items_due(conn):
    cal = calendars.list_calendars(conn)[0]
    calendars.create_entry(conn, calendar_id=cal["id"], title="Workshop", start="2026-10-06T09:00:00+02:00", end="2026-10-06T12:00:00+02:00")
    calendars.create_entry(conn, calendar_id=cal["id"], title="Lunch", start="2026-10-06T12:00:00+02:00", end="2026-10-06T13:00:00+02:00", show_as="free")
    hidden = calendars.create_calendar(conn, "Hidden")
    calendars.update_calendar(conn, hidden["id"], visible=False)
    calendars.create_entry(conn, calendar_id=hidden["id"], title="Not counted", start="2026-10-06T14:00:00+02:00", end="2026-10-06T16:00:00+02:00")
    work.create(conn, "Build", start_on="2026-10-05", due="2026-10-07")
    work.create(conn, "Report", due="2026-10-06")
    load = {x["day"]: x for x in timeline.timeline(conn, START, END)["load"]}
    assert (load[date(2026, 10, 6)]["busy_hours"], load[date(2026, 10, 6)]["active"], load[date(2026, 10, 6)]["due"]) == (3.0, 1, 1)
    assert load[date(2026, 10, 8)]["active"] == 0


def test_an_entry_split_off_from_an_item_shows_on_its_row(conn):
    wid = work.create(conn, "Cut-over", start_on="2026-10-10", due="2026-10-12")
    e = calendars.create_entry(conn, title="Cut-over window", start="2026-10-10T18:00:00+02:00", end="2026-10-10T22:00:00+02:00", work_item_id=wid)
    [row] = _binder(timeline.timeline(conn, START, END), "No home")["items"]
    assert [x["id"] for x in row["entries"]] == [e["id"]]


def test_the_api_gives_the_timeline_and_sets_a_binders_dates(conn, vault, database):
    p = objects.create(conn, "project", "Talos")
    work.create(conn, "Calendar", home_id=p, start_on="2026-10-01", due="2026-10-03")
    conn.commit()
    c = client(database, vault)
    d = c.get("/api/timeline?start=2026-10-01&end=2026-10-31").json()
    assert [b["name"] for b in d["binders"]] == ["Talos"] and len(d["load"]) == 31
    r = c.post(f"/api/objects/{p}", json={"starts_on": "2026-09-28", "ends_on": "2026-12-31"}, headers=H).json()
    assert (r["starts_on"], r["ends_on"]) == ("2026-09-28", "2026-12-31")
    assert c.get("/api/timeline?start=2026-10-31&end=2026-10-01").status_code == 400
    r = c.post("/api/work", json={"title": "Gantt", "home_id": p, "start_on": "2026-10-05", "due": "2026-10-20"}, headers=H)
    assert r.status_code == 201 and r.json()["start_on"] == "2026-10-05"
