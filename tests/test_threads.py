"""Conversations: the Messages filters over threads, a thread in order, paged, and a Teams chat."""

from datetime import datetime, timedelta, timezone

import mailfactory as mf
from test_teams import ANNA, BO, ME, T0, msg
from test_web import client

from talos import search
from talos.ingest import Location
from talos.teams_ingest import Conversation, TeamsIngestor

NOW = datetime.now(timezone.utc)


def _put(ingestor, account, thread, frm, *, minutes_ago, subject="Offert", to=mf.ME, automated=False, seen=True):
    at = NOW - timedelta(minutes=minutes_ago)
    raw = mf.make(frm=frm, to=to, subject=subject, date=at, body=f"{subject} text {minutes_ago}",
                  msgid=f"<{thread}-{minutes_ago}@test.invalid>",
                  headers={"Auto-Submitted": "auto-generated"} if automated else None)
    return ingestor.ingest(account, raw, Location("[all]", f"{thread}-{minutes_ago}", provider_thread_id=thread,
                                                  flags=["seen"] if seen else [], received_at=at)).message_id


def _conversations(ingestor, conn):
    """Three threads. A: Oskar twice and the owner's reply, the newest. B: one automated receipt.
    C: Pia twice in work, the oldest, one of them unread."""
    ids = {
        "a": [_put(ingestor, "gmail", "tA", "Oskar Nyström <oskar@nordvik.se>", minutes_ago=50),
              _put(ingestor, "gmail", "tA", "Oskar Nyström <oskar@nordvik.se>", minutes_ago=30),
              _put(ingestor, "gmail", "tA", f"Alex <{mf.ME}>", to="oskar@nordvik.se", minutes_ago=5,
                   subject="SV: Offert")],
        "b": [_put(ingestor, "gmail", "tB", "Klarna <noreply@klarna.com>", minutes_ago=20, subject="Kvitto",
                   automated=True)],
        "c": [_put(ingestor, "work", "tC", "Pia Ek <pia.ek@company.example>", minutes_ago=90, subject="Budget",
                   to="owner@company.example"),
              _put(ingestor, "work", "tC", "Pia Ek <pia.ek@company.example>", minutes_ago=80, subject="Budget",
                   to="owner@company.example", seen=False)],
    }
    conn.commit()
    tid = {k: conn.execute("select thread_id from message where id = %s", (v[0],)).fetchone()["thread_id"]
           for k, v in ids.items()}
    return ids, tid


def _agree(c, params):
    """The conversations listed, and the threads of the messages the same filters list."""
    threads = c.get("/api/threads", params=params).json()
    listed = c.get("/api/messages", params=params + [("limit", "500")]).json()
    from_messages = {r["thread_id"] for r in listed["rows"]}
    assert threads["total"] == len(threads["rows"]) == len(from_messages)
    return threads, from_messages


def test_the_conversation_list_has_one_row_per_thread_newest_activity_first(conn, ingestor, vault, database):
    ids, tid = _conversations(ingestor, conn)
    res = client(database, vault).get("/api/threads").json()
    assert res["total"] == 3
    assert [r["id"] for r in res["rows"]] == [tid["a"], tid["b"], tid["c"]]
    a, b, c = res["rows"]
    assert a["message_count"] == 3 and a["matched"] == 3 and a["mine"] == 1 and a["unread"] == 0
    assert a["latest_id"] == ids["a"][2] and a["direction"] == "out" and a["snippet"].startswith("SV: Offert text")
    assert [(p["name"], p["me"]) for p in a["people"]] == [("Oskar Nyström", False), ("Alex", True)]
    assert a["people_count"] == 2 and a["recipients"] == ["oskar@nordvik.se"]
    assert b["automated"] is True and b["mine"] == 0 and b["medium"] == "email"
    assert c["unread"] == 1 and c["account_id"] == "work" and c["people_count"] == 1


def test_a_thread_matches_when_any_of_its_messages_matches_and_the_counts_agree(conn, ingestor, vault, database):
    ids, tid = _conversations(ingestor, conn)
    c = client(database, vault)
    sent, threads = _agree(c, [("direction", "out")])
    assert threads == {tid["a"]} and sent["rows"][0]["matched"] == 1 and sent["rows"][0]["message_count"] == 3
    received, threads = _agree(c, [("direction", "in")])
    assert threads == {tid["a"], tid["b"], tid["c"]}
    # Hiding Oskar leaves thread A in the list only through the owner's reply, so not among received mail.
    hidden, threads = _agree(c, [("direction", "in"), ("exclude_senders", "oskar@nordvik.se")])
    assert threads == {tid["b"], tid["c"]}
    mine, threads = _agree(c, [("exclude_senders", "oskar@nordvik.se")])
    assert threads == {tid["a"], tid["b"], tid["c"]}
    assert _agree(c, [("exclude_accounts", "gmail")])[1] == {tid["c"]}
    assert _agree(c, [("accounts", "gmail"), ("automated", "false")])[1] == {tid["a"]}
    assert _agree(c, [("q", "Budget")])[1] == {tid["c"]}
    assert _agree(c, [("q", "SV")])[1] == {tid["a"]}  # the subject of one message finds the whole thread
    assert _agree(c, [("accounts", "work"), ("label", "Nope")])[1] == set()


def test_the_conversation_list_pages_like_the_messages_list(conn, ingestor, vault, database):
    _, tid = _conversations(ingestor, conn)
    c = client(database, vault)
    first = c.get("/api/threads", params={"limit": 2}).json()
    rest = c.get("/api/threads", params={"limit": 2, "offset": 2}).json()
    assert first["total"] == rest["total"] == 3
    assert [r["id"] for r in first["rows"] + rest["rows"]] == [tid["a"], tid["b"], tid["c"]]


def test_a_thread_opens_oldest_first_with_its_people_and_directions(conn, ingestor, vault, database):
    ids, tid = _conversations(ingestor, conn)
    c = client(database, vault)
    t = c.get(f"/api/threads/{tid['a']}").json()
    assert [m["id"] for m in t["messages"]] == ids["a"]
    assert [m["direction"] for m in t["messages"]] == ["in", "in", "out"]
    assert t["has_more"] is False and t["before"] is None and t["message_count"] == 3 and t["medium"] == "email"
    assert t["messages"][0]["text"] is None  # an e-mail's text is fetched when it is opened
    assert [(p["address"], p["me"], p["n"]) for p in t["people"]] == [("oskar@nordvik.se", False, 2), (mf.ME, True, 1)]
    assert t["messages"][1]["seen"] is True
    assert c.get("/api/threads/999999").status_code == 404
    assert c.get(f"/api/threads/{tid['a']}", params={"before": "x"}).status_code == 400


def test_a_long_thread_pages_back_from_the_newest(conn, ingestor, vault, database):
    ids = [_put(ingestor, "gmail", "long", "Anna Berg <anna.berg@company.example>", minutes_ago=100 - i) for i in range(7)]
    conn.commit()
    tid = conn.execute("select thread_id from message where id = %s", (ids[0],)).fetchone()["thread_id"]
    c = client(database, vault)
    one = c.get(f"/api/threads/{tid}", params={"limit": 3}).json()
    assert [m["id"] for m in one["messages"]] == ids[4:] and one["has_more"] and one["before"] == ids[4]
    two = c.get(f"/api/threads/{tid}", params={"limit": 3, "before": one["before"]}).json()
    assert [m["id"] for m in two["messages"]] == ids[1:4] and two["has_more"]
    three = c.get(f"/api/threads/{tid}", params={"limit": 3, "before": two["before"]}).json()
    assert [m["id"] for m in three["messages"]] == ids[:1] and not three["has_more"] and three["before"] is None
    # Messages at the same moment keep a stable order by id, so paging never skips or repeats one.
    conn.execute("update message set received_at = %s where thread_id = %s", (NOW, tid))
    conn.commit()
    seen, before = [], None
    while True:
        page = c.get(f"/api/threads/{tid}", params={"limit": 2, **({"before": before} if before else {})}).json()
        seen = [m["id"] for m in page["messages"]] + seen
        if not page["has_more"]:
            break
        before = page["before"]
    assert seen == sorted(ids)


def _chat(conn, ingestor):
    conn.execute("insert into account (id, provider, address) values ('teams', 'teams', 'teams@talos.invalid')")
    tin = TeamsIngestor(ingestor, "teams")
    tin.set_me(ME["id"])
    tin.learn_members([ME, ANNA, BO])
    conv = Conversation(kind="chat", key_prefix="chat:C9", thread_key="chat:C9", folder="Teams/Chats",
                        fallback_subject="Anna Berg, Bo Ek", member_ids=[ME["id"], ANNA["id"], BO["id"]],
                        meta={"chat_id": "C9", "chat_type": "group"})
    who = [ANNA, ANNA, ME, BO, ME]
    for i, w in enumerate(who):
        tin.ingest(conv, msg(f"x{i}", w, f"Rad {i}", T0 + timedelta(minutes=i), attachments=[
            {"id": "f1", "contentType": "reference", "name": "Plan.xlsx",
             "contentUrl": "https://kundbolaget.sharepoint.com/sites/x/Plan.xlsx"}] if i == 3 else []))
    conn.commit()
    return conn.execute("select id from thread where provider_thread_id = 'chat:C9'").fetchone()["id"]


def test_a_teams_chat_is_one_conversation_with_every_line_and_the_owners_own_marked(conn, ingestor, vault, database):
    tid = _chat(conn, ingestor)
    c = client(database, vault)
    listed = c.get("/api/threads", params={"accounts": "teams"}).json()
    assert listed["total"] == 1
    row = listed["rows"][0]
    assert row["id"] == tid and row["medium"] == "teams_chat" and row["chat_type"] == "group"
    assert row["subject"] == "Anna Berg, Bo Ek" and row["message_count"] == 5 and row["mine"] == 2
    assert [(p["name"], p["me"]) for p in row["people"]] == [("Anna Berg", False), ("Alex Lind", True), ("Bo Ek", False)]
    t = c.get(f"/api/threads/{tid}").json()
    assert t["medium"] == "teams_chat" and t["chat_type"] == "group"
    assert [m["text"] for m in t["messages"]] == [f"Rad {i}" for i in range(5)]
    assert [m["direction"] for m in t["messages"]] == ["in", "in", "out", "in", "out"]
    assert [a["filename"] for a in t["messages"][3]["attachments"]] == ["Plan.xlsx"]
    assert t["messages"][3]["attachments"][0]["attrs"]["url"].startswith("https://")
    assert {p["name"]: p["n"] for p in t["people"]} == {"Anna Berg": 2, "Alex Lind": 2, "Bo Ek": 1}
    # Hiding Teams leaves the chat out; a search for one line finds the whole chat.
    assert c.get("/api/threads", params={"exclude_accounts": "teams"}).json()["total"] == 0
    found = c.get("/api/threads", params={"q": "Rad", "medium": "teams"}).json()
    assert found["total"] == 1 and found["rows"][0]["matched"] == 5
    back = c.get(f"/api/threads/{tid}", params={"limit": 2, "before": t["messages"][2]["id"]}).json()
    assert [m["text"] for m in back["messages"]] == ["Rad 0", "Rad 1"] and not back["has_more"]


def test_the_thread_list_query_takes_every_messages_filter(conn):
    sql, params = search.threads_sql("faktura", accounts=["gmail"], exclude_senders=["a@b.se"], direction="in",
                                     dimension=[("type", "receipt")], important=True, limit=10, offset=20,
                                     sort="importance", select="m.id")
    assert "group by 1" in sql and params["t_limit"] == 10 and params["t_offset"] == 20
    conn.execute("explain " + sql, params)
