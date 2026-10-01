"""A binder's activity log, its facts and notes, and editing its own text."""

from datetime import datetime, timedelta, timezone

import mailfactory as mf
import pytest
from psycopg.types.json import Jsonb
from starlette.testclient import TestClient

from talos import activity, objects, personal, work
from talos.config import Settings
from talos.ingest import Location
from talos.web import app

H = {"X-Talos": "1"}
T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


def client(database, vault):
    return TestClient(app.create(Settings(home=vault.root.parent, dsn=database), allowed_hosts=["testserver"]))


def mail(ingestor, key, subject, at, frm="Oskar <oskar@nordvik.se>"):
    return ingestor.ingest("gmail", mf.make(frm=frm, subject=subject, date=at),
                           Location("[all]", key, provider_id=key, received_at=at)).message_id


def note(conn, object_id, kind, title, created, updated=None):
    nid = conn.execute("insert into entity (kind) values ('note') returning id").fetchone()["id"]
    conn.execute("insert into note (id, object_id, kind, title, body, created_at, updated_at) values (%s, %s, %s, %s, '', %s, %s)",
                 (nid, object_id, kind, title, created, updated or created))
    return nid


def at_event(conn, work_item_id, field, when):
    conn.execute("update work_item_event set at = %s where work_item_id = %s and field = %s", (when, work_item_id, field))


@pytest.fixture
def binder(conn, ingestor):
    """A project with a work item that moved, mail of three kinds, notes and an excluded message."""
    oid = objects.create(conn, "project", "Harbour Migration", attrs={"created": "2026-08-01", "lifecycle": "active"})
    conn.execute("update object set origin = %s where id = %s",
                 (Jsonb({"source": "obsidian", "path": "Projects/Harbour/_home.md", "imported_at": "2026-08-02T08:00:00+00:00"}), oid))
    member = mail(ingestor, "m1", "Cutover plan", T0 + timedelta(days=1))
    about = mail(ingestor, "m2", "Cutover date?", T0 + timedelta(days=3))
    kept_out = mail(ingestor, "m3", "Unrelated newsletter", T0 + timedelta(days=4))
    mail(ingestor, "m4", "Somebody else's mail", T0 + timedelta(days=5))
    objects.add(conn, oid, [member, kept_out])
    objects.exclude(conn, oid, [kept_out])
    wid = work.create(conn, "Confirm the cutover date", home_id=oid, created_at=T0, message_ids=[about])
    work.update(conn, wid, status="doing", position=1.0)
    at_event(conn, wid, "status", T0 + timedelta(days=2))
    at_event(conn, wid, "position", T0 + timedelta(days=2))
    elsewhere = objects.create(conn, "area", "Security")
    work.create(conn, "Someone else's item", home_id=elsewhere, created_at=T0 + timedelta(days=6))
    decision = note(conn, oid, "decision", "Kickoff decisions", T0 + timedelta(hours=12))
    state = note(conn, oid, "state", "Recent state", T0 + timedelta(days=2, hours=1), updated=T0 + timedelta(days=7))
    return {"id": oid, "work": wid, "member": member, "about": about, "kept_out": kept_out,
            "decision": decision, "state": state}


def test_the_activity_log_merges_work_messages_notes_and_the_object_newest_first(conn, binder):
    items = activity.activity(conn, binder["id"])["items"]
    assert [(x["type"], x["event"], x["title"]) for x in items] == [
        ("note", "updated", "Recent state"),
        ("message", "received", "Cutover date?"),
        ("note", "created", "Recent state"),
        ("work", "status", "Confirm the cutover date"),     # the reorder written with the move is left out
        ("message", "received", "Cutover plan"),            # the excluded and the unlinked mail are not here
        ("note", "created", "Kickoff decisions"),
        ("work", "created", "Confirm the cutover date"),
        ("object", "imported", "Harbour Migration"),
        ("object", "created", "Harbour Migration"),
    ]
    assert items[-1]["at"] == datetime(2026, 8, 1, tzinfo=timezone.utc)  # the vault's created date
    assert items[-2]["path"] == "Projects/Harbour/_home.md"
    move = next(x for x in items if x["event"] == "status")
    assert (move["old_value"], move["new_value"], move["by"]) == ("inbox", "doing", personal.OWNER_ID)
    via = {x["title"]: x["via_work"] for x in items if x["type"] == "message"}
    assert via == {"Cutover date?": ["Confirm the cutover date"], "Cutover plan": []}
    created = next(x for x in items if x["type"] == "work" and x["event"] == "created")
    assert (created["created_status"], created["by"], created["work_item_id"]) == ("inbox", personal.OWNER_ID, binder["work"])


def test_the_filters_show_only_their_stream(conn, binder):
    def types(show):
        return {(x["type"], x["event"]) for x in activity.activity(conn, binder["id"], show=show)["items"]}

    assert types("work") == {("work", "created"), ("work", "status")}
    assert types("messages") == {("message", "received")}
    assert types("notes") == {("note", "created"), ("note", "updated")}
    with pytest.raises(activity.ActivityError):
        activity.activity(conn, binder["id"], show="gossip")


def test_the_cursor_pages_through_everything_once_even_when_items_share_a_timestamp(conn, binder):
    # An import writes many items at one instant; a page break inside them must not lose any.
    oid = binder["id"]
    same = T0 + timedelta(days=10)
    for i in range(5):
        work.create(conn, f"Imported item {i}", home_id=oid, created_at=same)
    everything = [x["key"] for x in activity.activity(conn, oid, limit=200)["items"]]
    assert len(everything) == len(set(everything)) == 14
    paged, before = [], None
    while True:
        page = activity.activity(conn, oid, limit=2, before=before)
        paged += [x["key"] for x in page["items"]]
        before = page["next"]
        if not before:
            break
    assert paged == everything
    # A bare timestamp works as a cursor too: everything before that instant.
    older = activity.activity(conn, oid, before=(T0 + timedelta(days=2)).isoformat())["items"]
    assert all(x["at"] < T0 + timedelta(days=2) for x in older) and len(older) == 5
    with pytest.raises(activity.ActivityError):
        activity.decode_cursor("yesterday-ish")


def test_the_activity_api_orders_filters_and_pages(conn, binder, vault, database):
    conn.commit()
    c = client(database, vault)
    oid = binder["id"]
    r = c.get(f"/api/objects/{oid}/activity")
    assert r.status_code == 200
    body = r.json()
    assert body["next"] is None and len(body["items"]) == 9
    ats = [datetime.fromisoformat(x["at"]) for x in body["items"]]
    assert ats == sorted(ats, reverse=True)
    assert {x["type"] for x in c.get(f"/api/objects/{oid}/activity", params={"show": "messages"}).json()["items"]} == {"message"}
    first = c.get(f"/api/objects/{oid}/activity", params={"limit": 4}).json()
    assert len(first["items"]) == 4 and first["next"] == first["items"][-1]["key"]
    second = c.get(f"/api/objects/{oid}/activity", params={"limit": 4, "before": first["next"]}).json()
    assert [x["key"] for x in first["items"] + second["items"]] == [x["key"] for x in body["items"][:8]]
    assert c.get(f"/api/objects/{oid}/activity", params={"show": "nonsense"}).status_code == 400
    assert c.get(f"/api/objects/{oid}/activity", params={"before": "not a cursor"}).status_code == 400
    assert c.get("/api/objects/424242/activity").status_code == 404

    d = c.get(f"/api/objects/{oid}").json()
    assert d["facts"] == {"work_open": 1, "work_done": 0, "messages": 2, "notes": 2}
    assert [n["title"] for n in d["notes"]] == ["Recent state", "Kickoff decisions"]


def test_a_stored_query_and_thread_members_count_as_linked_mail(conn, ingestor):
    a = mail(ingestor, "q1", "Offert", T0, frm="Anna <anna@nordvik.se>")
    b = mail(ingestor, "q2", "Kvitto", T0 + timedelta(days=1), frm="Klarna <noreply@klarna.com>")
    thread = conn.execute("select thread_id from message where id = %s", (b,)).fetchone()["thread_id"]
    oid = objects.create(conn, "project", "Nordvik", query=[{"field": "from_domain", "op": "is", "value": "nordvik.se"}])
    objects.add(conn, oid, [thread])
    got = [x["message_id"] for x in activity.activity(conn, oid, show="messages")["items"]]
    assert got == [b, a]
    assert activity.facts(conn, oid)["messages"] == 2


def test_set_body_replaces_the_text_and_keeps_an_imported_description_in_step(conn):
    oid = objects.create(conn, "project", "Harbour", description="Move the file shares.")
    conn.execute("update object set body = %s where id = %s",
                 ("## Purpose\n\nMove the file shares.\n\n## Outcome\n\nShares moved.\n", oid))
    objects.set_body(conn, oid, "## Purpose\r\n\r\nMove the shares to the new NAS.\n\n## Outcome\n\nDone.\n\n\n")
    row = objects.get(conn, oid)
    assert row["body"] == "## Purpose\n\nMove the shares to the new NAS.\n\n## Outcome\n\nDone.\n"
    assert row["description"] == "Move the shares to the new NAS."  # it was the Purpose section, so it follows

    objects.describe(conn, oid, "Written by hand")
    objects.set_body(conn, oid, "## Purpose\n\nSomething else.\n")
    assert objects.get(conn, oid)["description"] == "Written by hand"  # a hand-written description stays
    objects.set_body(conn, oid, None)
    assert objects.get(conn, oid)["body"] == ""
    with pytest.raises(objects.ObjectError):
        objects.set_body(conn, oid, ["not", "text"])
    with pytest.raises(objects.ObjectError):
        objects.set_body(conn, 424242, "x")


def test_the_object_api_edits_the_body_only_with_the_header(conn, vault, database):
    oid = objects.create(conn, "area", "Security")
    conn.commit()
    c = client(database, vault)
    assert c.post(f"/api/objects/{oid}", json={"body": "Hijacked"}).status_code == 403
    r = c.post(f"/api/objects/{oid}", json={"body": "## What belongs here\n\n- firewalls\n- alarms"}, headers=H)
    assert r.status_code == 200 and r.json()["body"] == "## What belongs here\n\n- firewalls\n- alarms\n"
    assert c.post(f"/api/objects/{oid}", json={"body": 42}, headers=H).status_code == 400
    assert c.get(f"/api/objects/{oid}").json()["body"].startswith("## What belongs here")


def test_board_rows_name_their_related_binders(conn):
    home = objects.create(conn, "project", "Harbour")
    rel = objects.create(conn, "system", "Nimbus NAS")
    wid = work.create(conn, "Check the disks", home_id=home)
    work.relate(conn, wid, rel)
    row = work.list_items(conn, home_id=home)[0]
    assert row["related"] == [{"id": rel, "name": "Nimbus NAS", "kind": "system"}]
    assert work.list_items(conn, home_id="none") == []
