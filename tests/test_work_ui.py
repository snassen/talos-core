"""The work item API behind the Work view, the object Board, the drawers and Needs attention."""

from datetime import date, timedelta

import mailfactory as mf
from starlette.testclient import TestClient

from talos import objects, work
from talos.config import Settings
from talos.ingest import Location
from talos.web import app

H = {"X-Talos": "1"}


def client(database, vault):
    return TestClient(app.create(Settings(home=vault.root.parent, dsn=database), allowed_hosts=["testserver"]))


def test_the_api_creates_updates_and_moves_a_work_item(conn, vault, database):
    project = objects.create(conn, "project", "FortiDLP Renewal")
    conn.commit()
    c = client(database, vault)
    r = c.post("/api/work", json={"title": "  Ask for the renewal quote ", "home_id": project, "due": "2026-10-01",
                                  "focus": True, "body": "Outcome: a signed quote.\n\n- 25 seats"}, headers=H)
    assert r.status_code == 201
    item = r.json()
    wid = item["id"]
    assert (item["title"], item["status"], item["home_name"], item["home_kind"], item["due"], item["focus"]) == (
        "Ask for the renewal quote", "inbox", "FortiDLP Renewal", "project", "2026-10-01", True)

    r = c.post(f"/api/work/{wid}", json={"title": "Get the renewal quote", "review_after": "2026-09-28"}, headers=H)
    assert r.status_code == 200 and r.json()["title"] == "Get the renewal quote"

    other = c.post("/api/work", json={"title": "Already next", "status": "next"}, headers=H).json()
    r = c.post(f"/api/work/{wid}", json={"status": "next"}, headers=H)  # a move menu: to the end of the column
    assert r.json()["status"] == "next" and r.json()["position"] > other["position"]
    r = c.post(f"/api/work/{wid}", json={"status": "doing", "position": 0.5}, headers=H)  # a drag and drop
    assert (r.json()["status"], r.json()["position"]) == ("doing", 0.5)

    detail = c.get(f"/api/work/{wid}").json()
    fields = [e["field"] for e in detail["history"]]
    assert fields[0] == "created" and fields.count("status") == 2 and "title" in fields and "review_after" in fields
    listed = c.get("/api/work", params={"status": "doing"}).json()
    assert [(w["id"], w["home_name"], w["home_kind"], w["message_count"]) for w in listed] == [
        (wid, "FortiDLP Renewal", "project", 0)]
    assert [w["id"] for w in c.get("/api/work", params={"home": project}).json()] == [wid]
    assert [w["id"] for w in c.get("/api/work", params={"home": "none"}).json()] == [other["id"]]
    assert [w["id"] for w in c.get("/api/work", params={"q": "renewal"}).json()] == [wid]
    assert [w["id"] for w in c.get("/api/work", params={"focus": "1"}).json()] == [wid]
    assert c.get("/api/work/999999").status_code == 404


def test_needs_attention_returns_the_right_items_each_with_its_reasons(conn, vault, database):
    today = date.today()
    ids = {
        "inbox": work.create(conn, "Just filed"),
        "doing": work.create(conn, "Working on it", status="doing"),
        "blocked": work.create(conn, "Waiting on Telia", status="blocked"),
        "focus": work.create(conn, "Focus this week", status="next", focus=True),
        "overdue": work.create(conn, "Late", status="next", due=today - timedelta(days=2)),
        "review": work.create(conn, "Look again", status="someday", review_after=today),
        "calm": work.create(conn, "Nothing urgent", status="next", due=today + timedelta(days=5)),
        "later": work.create(conn, "Not yet", status="someday", review_after=today + timedelta(days=1)),
        "done": work.create(conn, "Finished but was late", status="done", focus=True, due=today - timedelta(days=9)),
    }
    both = work.create(conn, "Late and blocked", status="blocked", due=today - timedelta(days=1))
    conn.commit()
    rows = client(database, vault).get("/api/work/attention").json()
    got = {r["id"]: r["reasons"] for r in rows}
    assert got == {ids["inbox"]: ["inbox"], ids["doing"]: ["doing"], ids["blocked"]: ["blocked"],
                   ids["focus"]: ["focus"], ids["overdue"]: ["overdue"], ids["review"]: ["review"],
                   both: ["overdue", "blocked"]}
    texts = {r["id"]: r["reason_text"] for r in rows}
    assert texts[ids["doing"]] == ["in progress"] and texts[ids["review"]] == ["review due"]
    assert rows[0]["reasons"][0] == "overdue" and rows[-1]["reasons"] == ["inbox"]  # strongest reason first
    overdue = client(database, vault).get("/api/work", params={"overdue": "1"}).json()
    assert {r["id"] for r in overdue} == {ids["overdue"], both}


def test_making_a_work_item_from_a_message_links_them_both_ways(conn, ingestor, vault, database):
    mid = ingestor.ingest("gmail", mf.make(subject="Brandväggsfönster fredag?"), Location("[all]", "m1")).message_id
    other = ingestor.ingest("gmail", mf.make(subject="Kärnswitcharna"), Location("[all]", "m2")).message_id
    area = objects.create(conn, "area", "Security")
    conn.commit()
    c = client(database, vault)
    r = c.post("/api/work", json={"title": "Brandväggsfönster fredag?", "home_id": area, "message_ids": [mid]}, headers=H)
    assert r.status_code == 201
    wid = r.json()["id"]
    assert [m["id"] for m in r.json()["messages"]] == [mid]
    m = c.get(f"/api/messages/{mid}").json()
    assert [(w["id"], w["title"], w["status"], w["home_name"]) for w in m["work"]] == [
        (wid, "Brandväggsfönster fredag?", "inbox", "Security")]
    assert c.get("/api/work").json()[0]["message_count"] == 1

    r = c.post(f"/api/work/{wid}/messages", json={"add": [other], "remove": [mid]}, headers=H)
    assert [x["id"] for x in r.json()["messages"]] == [other]
    assert c.get(f"/api/messages/{mid}").json()["work"] == []
    assert [w["id"] for w in c.get(f"/api/messages/{other}").json()["work"]] == [wid]
    assert c.get(f"/api/work/{wid}").json()["history"][-1]["field"] == "messages"
    bad = c.post(f"/api/work/{wid}/messages", json={"add": [area]}, headers=H)  # an object is not a message
    assert bad.status_code == 400 and "no message" in bad.json()["error"]
    assert c.post("/api/work", json={"title": "x", "message_ids": [987654]}, headers=H).status_code == 400


def test_a_post_without_the_header_is_refused(conn, vault, database):
    wid = work.create(conn, "Something")
    conn.commit()
    c = client(database, vault)
    assert c.post("/api/work", json={"title": "Sneaky"}).status_code == 403
    assert c.post(f"/api/work/{wid}", json={"status": "done"}).status_code == 403
    assert c.post(f"/api/work/{wid}/messages", json={"add": []}).status_code == 403
    assert conn.execute("select status from work_item where id = %s", (wid,)).fetchone()["status"] == "inbox"
    assert conn.execute("select count(*) n from work_item").fetchone()["n"] == 1


def test_an_invalid_status_or_date_is_a_400_with_a_message_never_a_500(conn, vault, database):
    wid = work.create(conn, "Something")
    conn.commit()
    c = client(database, vault)
    cases = [
        (f"/api/work/{wid}", {"status": "waiting"}, "unknown status"),
        (f"/api/work/{wid}", {"due": "nästa vecka"}, "due must be a date"),
        (f"/api/work/{wid}", {"review_after": "2026-13-45"}, "review_after must be a date"),
        (f"/api/work/{wid}", {"home_id": 999999999}, "no object"),
        (f"/api/work/{wid}", {"title": "   "}, "needs a title"),
        (f"/api/work/{wid}", {"focus": "yes"}, "focus is true or false"),
        (f"/api/work/{wid}", {"position": "top"}, "position is a number"),
        (f"/api/work/{wid}", {"done_at": "2026-01-01"}, "cannot set done_at"),
        ("/api/work", {"title": "New", "status": 3}, "unknown status"),
        ("/api/work", {"title": "New", "due": "tomorrow"}, "due must be a date"),
        ("/api/work", {"status": "next"}, "needs a title"),
    ]
    for path, body, message in cases:
        r = c.post(path, json=body, headers=H)
        assert r.status_code == 400, (body, r.status_code, r.text)
        assert message in r.json()["error"], (body, r.json())
    assert c.get("/api/work", params={"status": "waiting"}).status_code == 400
    assert c.post(f"/api/work/{wid}", content=b"not json", headers=H).status_code == 400
    row = conn.execute("select status, due, title from work_item where id = %s", (wid,)).fetchone()
    assert (row["status"], row["due"], row["title"]) == ("inbox", None, "Something")
    assert len(work.get(conn, wid)["history"]) == 1  # nothing half-written


def test_the_board_order_follows_position(conn, vault, database):
    system = objects.create(conn, "system", "Check Point")
    a = work.create(conn, "A", status="next", home_id=system)
    b = work.create(conn, "B", status="next", home_id=system)
    d = work.create(conn, "D", status="next", home_id=system)
    first = work.create(conn, "Inbox first", home_id=system)
    conn.commit()
    c = client(database, vault)
    assert [w["title"] for w in c.get("/api/work", params={"home": system}).json()] == ["Inbox first", "A", "B", "D"]
    # Drag D between A and B: the client sends the midpoint of its neighbours.
    pa, pb = (c.get(f"/api/work/{i}").json()["position"] for i in (a, b))
    c.post(f"/api/work/{d}", json={"position": (pa + pb) / 2}, headers=H)
    assert [w["id"] for w in c.get("/api/work", params={"status": "next"}).json()] == [a, d, b]
    # The object page shows the same board, in the same order.
    board = c.get(f"/api/objects/{system}").json()["work"]
    assert [w["id"] for w in board] == [first, a, d, b]
    homes = {h["id"]: h for h in c.get("/api/work/homes").json()}
    assert homes[system]["work_open"] == 4 and homes[system]["kind"] == "system"


def test_a_move_on_the_work_board_holds_between_items_of_different_homes(conn, vault, database):
    homes = [objects.create(conn, "area", name) for name in ("Security", "Costs", "Admin")]
    x, y, z = (work.create(conn, t, status="next", home_id=hid) for t, hid in zip("XYZ", homes))
    conn.commit()
    c = client(database, vault)
    px, py = (c.get(f"/api/work/{i}").json()["position"] for i in (x, y))
    assert px != py  # one column across every home, so there is room between them
    c.post(f"/api/work/{z}", json={"position": (px + py) / 2}, headers=H)
    assert [w["id"] for w in c.get("/api/work", params={"status": "next"}).json()] == [x, z, y]
