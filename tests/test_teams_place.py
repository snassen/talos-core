"""Teams as a place of its own: the kinds told apart, a found message opened where it was said, the fast lane,
one channel read after a post, posting on the owner's Enter with a one-time token (promise 1), and the boost after a post.
All data is invented."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from talos import search, send, syncnow
from talos.config import Settings
from talos.web import app
from test_teams import FakeTeams, run, source, teams_conn  # noqa: F401 — teams_conn is a fixture

SAME = {"X-Talos": "1", "Sec-Fetch-Site": "same-origin"}


def _synced(conn, ingestor):
    fake = FakeTeams()
    run(conn, ingestor, source(fake))
    conn.commit()
    return fake


def test_the_kinds_are_told_apart_one_to_one_group_and_channel(teams_conn, ingestor):
    _synced(teams_conn, ingestor)
    n = lambda **f: search.messages(teams_conn, None, medium="teams", **f)["total"]  # noqa: E731
    assert n(chat="oneOnOne") == 1 and n(chat="group") == 5 and n(chat="channel") == 6 and n(chat="meeting") == 0
    assert n(team="Kundbolaget") == 6 and n(team="Kundbolaget", channel="Allmänt") == 6 and n(team="Annat") == 0
    rows = search.threads(teams_conn, None, medium="teams", chat="channel")["rows"]
    assert {(r["team"], r["channel"]) for r in rows} == {("Kundbolaget", "Allmänt")}


def test_mail_only_lists_email_when_its_medium_is_email(teams_conn, ingestor):
    _synced(teams_conn, ingestor)
    assert search.messages(teams_conn, None, medium="email")["total"] == 0


def test_a_found_message_opens_with_the_messages_around_it(teams_conn, ingestor):
    _synced(teams_conn, ingestor)
    tid = teams_conn.execute("select thread_id from message where headers->>'x-teams-chat-type' = 'group' limit 1").fetchone()["thread_id"]
    ids = [r["id"] for r in teams_conn.execute("select id from message where thread_id = %s order by received_at, id", (tid,))]
    t = search.thread(teams_conn, tid, around=ids[2], limit=2)
    assert [m["id"] for m in t["messages"]] == ids[1:4] and t["has_more"] and t["has_later"] and t["around"] == ids[2]
    later = search.thread(teams_conn, tid, after=t["after"], limit=2)
    assert [m["id"] for m in later["messages"]] == ids[4:6] and not later["has_later"]


def test_the_fast_lane_asks_for_the_newest_chats_once_and_never_for_channels(teams_conn, ingestor):
    fake = FakeTeams()
    run(teams_conn, ingestor, source(fake, recent=20))
    listed = [u for u in fake.requests if "/me/chats" in u]
    assert len(listed) == 1 and "orderby=lastMessagePreview" in listed[0].replace("%2F", "/").replace("$", "")
    assert not any("/teams/" in u or "joinedTeams" in u for u in fake.requests)


def test_one_channel_is_read_alone_after_a_post_in_it(teams_conn, ingestor):
    fake = FakeTeams()
    run(teams_conn, ingestor, source(fake, only_channel={"team": {"id": "T1", "displayName": "Kundbolaget"},
                                                        "channel": {"id": "CH1", "displayName": "Allmänt"}}))
    assert not any("/me/chats" in u or "joinedTeams" in u for u in fake.requests)
    assert teams_conn.execute("select count(*) n from message where medium = 'teams_channel'").fetchone()["n"] == 6


class FakePostTransport:
    def __init__(self):
        self.posts = []

    def post(self, p):
        self.posts.append((p.path, p.text))
        return {"id": "new-1"}


def _sender(transport):
    return send.Sender(None, book=send.Confirmations(dwell=0), teams_transport=lambda conn: transport)


def test_a_post_goes_only_with_its_confirmation_to_exactly_that_chat_and_text(teams_conn, ingestor):
    _synced(teams_conn, ingestor)
    tid = teams_conn.execute("select thread_id from message where headers->>'x-teams-chat-id' = 'C1' limit 1").fetchone()["thread_id"]
    tr = FakePostTransport()
    s = _sender(tr)
    with pytest.raises(send.SendRefused, match="confirmation"):
        s.teams_send(teams_conn, None, thread_id=tid, text="Hej")
    c = s.teams_confirm(teams_conn, thread_id=tid, text="Hej allihop")
    assert c["where"] == "the chat “Projekt Norrsken”"
    with pytest.raises(send.SendRefused, match="changed"):
        s.teams_send(teams_conn, c["token"], thread_id=tid, text="Hej allihop!")
    c = s.teams_confirm(teams_conn, thread_id=tid, text="Hej allihop")
    assert s.teams_send(teams_conn, c["token"], thread_id=tid, text="Hej allihop")["sent"]
    assert tr.posts == [("/chats/C1/messages", "Hej allihop")]
    with pytest.raises(send.SendRefused):
        s.teams_send(teams_conn, c["token"], thread_id=tid, text="Hej allihop")  # a token is used once
    logged = teams_conn.execute("select result, subject from send_log where account_id = 'teams' order by id").fetchall()
    assert [r["result"] for r in logged] == ["refused", "refused", "sent", "refused"]
    assert "Hej" not in json.dumps([dict(r) for r in logged], default=str)  # the text is never logged


def test_a_reply_in_a_channel_thread_goes_to_its_root_and_a_new_post_to_the_channel(teams_conn, ingestor):
    _synced(teams_conn, ingestor)
    tid = teams_conn.execute("select thread_id from message where headers->>'x-teams-root-id' = 'R1' limit 1").fetchone()["thread_id"]
    tr = FakePostTransport()
    s = _sender(tr)
    c = s.teams_confirm(teams_conn, thread_id=tid, text="Klart")
    assert c["where"].startswith("Kundbolaget › Allmänt, a reply to")
    s.teams_send(teams_conn, c["token"], thread_id=tid, text="Klart")
    c = s.teams_confirm(teams_conn, team="Kundbolaget", channel="Allmänt", text="Ny tråd")
    s.teams_send(teams_conn, c["token"], team="Kundbolaget", channel="Allmänt", text="Ny tråd")
    assert tr.posts == [("/teams/T1/channels/CH1/messages/R1/replies", "Klart"), ("/teams/T1/channels/CH1/messages", "Ny tråd")]


def test_a_teams_token_can_be_used_at_once_while_a_mail_confirmation_still_waits(teams_conn, ingestor):
    """The owner's Enter in the conversation's box is the confirmation: no dwell for a Teams post."""
    _synced(teams_conn, ingestor)
    tid = teams_conn.execute("select thread_id from message where headers->>'x-teams-chat-id' = 'C1' limit 1").fetchone()["thread_id"]
    tr = FakePostTransport()
    book = send.Confirmations(clock=lambda: 50.0)  # the clock stands still: no time passes between the two
    s = send.Sender(None, book=book, teams_transport=lambda conn: tr)
    c = s.teams_confirm(teams_conn, thread_id=tid, text="Snart där")
    assert c["wait"] == 0
    assert s.teams_send(teams_conn, c["token"], thread_id=tid, text="Snart där")["sent"]
    token = book.issue(7, "a mail's digest")
    with pytest.raises(send.SendRefused) as e:
        book.redeem(token, 7, "a mail's digest")
    assert e.value.code == "too_fast" and book.dwell == send.MIN_DWELL > 0


def test_teams_posts_have_their_own_hourly_limit_and_never_use_up_the_mails(teams_conn, ingestor):
    _synced(teams_conn, ingestor)
    tid = teams_conn.execute("select thread_id from message where headers->>'x-teams-chat-id' = 'C1' limit 1").fetchone()["thread_id"]
    teams_conn.execute("delete from send_log")
    teams_conn.execute("insert into send_log (account_id, result, subject, by_whom) select 'teams', 'sent', 'x', 'owner'"
                       " from generate_series(1, %s)", (send.RATE_LIMIT + 5,))
    assert send.sends_in_window(teams_conn) == 0 and send.sends_in_window(teams_conn, teams=True) == send.RATE_LIMIT + 5
    send.check_rate(teams_conn)  # the mails are not held up by the chatting
    s = _sender(FakePostTransport())
    s.teams_confirm(teams_conn, thread_id=tid, text="Ok")  # nor is Teams, below its own limit
    teams_conn.execute("insert into send_log (account_id, result, subject, by_whom) select 'teams', 'sent', 'x', 'owner'"
                       " from generate_series(1, %s)", (send.TEAMS_RATE_LIMIT,))
    with pytest.raises(send.SendRefused) as e:
        s.teams_confirm(teams_conn, thread_id=tid, text="Ok")
    assert e.value.code == "rate_limit" and "Teams" in str(e.value)


def test_the_web_posts_only_from_this_page_after_the_confirmation(teams_conn, ingestor, vault, database):
    _synced(teams_conn, ingestor)
    tid = teams_conn.execute("select thread_id from message where headers->>'x-teams-chat-id' = 'C2' limit 1").fetchone()["thread_id"]
    tr = FakePostTransport()
    runs = []
    c = TestClient(app.create(Settings(home=vault.root.parent, dsn=database), allowed_hosts=["testserver"],
                              send_book=send.Confirmations(dwell=0), teams_transport=lambda conn: tr,
                              sync_runner=lambda cmd, **kw: runs.append(cmd) or type("P", (), {"poll": lambda self: 0, "returncode": 0})()))
    assert c.post("/api/teams/confirm", json={"thread_id": tid, "text": "Tack"}, headers={"X-Talos": "1"}).status_code == 403
    conf = c.post("/api/teams/confirm", json={"thread_id": tid, "text": "Tack"}, headers=SAME).json()
    assert c.post("/api/teams/send", json={"thread_id": tid, "text": "Tack"}, headers=SAME).status_code == 403  # no token
    r = c.post("/api/teams/send", json={"thread_id": tid, "text": "Tack", "token": conf["token"]}, headers=SAME)
    assert r.status_code == 200 and tr.posts == [("/chats/C2/messages", "Tack")]
    assert runs and runs[-1][-2:] == ["--recent", str(syncnow.TEAMS_RECENT)]  # fetched back at once
    assert c.get("/api/teams/refresh").json()["boost"] > 100  # and looked for often, for a while
    places = c.get("/api/teams/places").json()
    assert places["chats"] == {"group": 1, "oneOnOne": 1} and places["teams"][0]["channels"][0]["channel"] == "Allmänt"


def test_the_fast_lane_runs_one_at_a_time_and_not_too_often(tmp_path):
    started = []

    class Proc:
        def poll(self):
            return 0
        returncode = 0
    now = [1000.0]
    fast = syncnow.TeamsFast(tmp_path, runner=lambda cmd, **kw: started.append(cmd) or Proc(), clock=lambda: now[0])
    fast.start()
    fast.start()
    now[0] += syncnow.TEAMS_FAST_EVERY + 1
    fast.start(channel=("T1", "CH1"))
    assert [c[-2:] for c in started] == [["--recent", str(syncnow.TEAMS_RECENT)], ["--channel", "T1/CH1"]]


def test_a_fast_lane_run_that_finds_nothing_leaves_no_sync_record_and_one_that_finds_something_does(teams_conn, ingestor):
    from talos.sources import base
    fake = FakeTeams()
    base.run(teams_conn, ingestor, "teams", source(fake, recent=20), quiet=True)
    n = lambda: teams_conn.execute("select count(*) n from sync_run where account_id = 'teams'").fetchone()["n"]  # noqa: E731
    assert n() == 1  # the first look brought messages in
    base.run(teams_conn, ingestor, "teams", source(fake, recent=20), quiet=True)
    assert n() == 1  # nothing new: no row


def test_the_fast_lane_checks_in_with_argus_at_most_every_four_minutes(teams_conn, monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone

    from psycopg.types.json import Jsonb

    from talos import argus, cli
    pings = []
    monkeypatch.setattr(argus, "ping", lambda dsn, slug, **kw: pings.append((slug, kw["ok"])) or True)
    s = type("S", (), {"dsn": "x"})()
    cli._teams_fast_done(teams_conn, s, ok=True, added=2)
    cli._teams_fast_done(teams_conn, s, ok=True, added=0)
    assert pings == [("talos-teams", True)]
    old = (datetime.now(timezone.utc) - timedelta(seconds=cli.TEAMS_PING_EVERY + 1)).isoformat()
    teams_conn.execute("update sync_cursor set state = state || %s where account_id = 'teams' and scope = 'fast'",
                       (Jsonb({"pinged_at": old}),))
    cli._teams_fast_done(teams_conn, s, ok=True, added=0)
    cli._teams_fast_done(teams_conn, s, ok=False, added=0)  # a failure is always told
    assert pings == [("talos-teams", True), ("talos-teams", True), ("talos-teams", False)]
    st = teams_conn.execute("select state from sync_cursor where account_id = 'teams' and scope = 'fast'").fetchone()["state"]
    assert st["checked_at"] and st["ok"] is False


def test_the_teams_service_runs_the_fast_lane_every_twenty_seconds():
    from talos import cli
    plist = cli.TEAMS_LAUNCHD.format(exe="/x/talos", logs="/l", env="", prefix="local.talos")
    assert "<string>local.talos.teams</string>" in plist and "<integer>20</integer>" in plist
    assert "<string>sync</string><string>teams</string><string>--recent</string><string>20</string>" in plist


class _Done:
    def poll(self):
        return 0
    returncode = 0


def test_a_post_boosts_the_fast_lane_to_every_five_seconds_until_it_cools_down(tmp_path):
    started = []
    now = [1000.0]
    fast = syncnow.TeamsFast(tmp_path, runner=lambda cmd, **kw: started.append(cmd) or _Done(), clock=lambda: now[0])
    fast.start()
    now[0] += syncnow.TEAMS_BOOST_EVERY + 1
    fast.start()  # not boosted: too soon
    assert len(started) == 1 and fast.state()["boost"] == 0
    fast.boost()
    assert fast.state()["boost"] == syncnow.TEAMS_BOOST_FOR
    fast.start()
    now[0] += syncnow.TEAMS_BOOST_EVERY + 1
    fast.start()
    assert len(started) == 3
    now[0] += syncnow.TEAMS_BOOST_FOR  # no answer, no new post: cooled down
    fast.start()
    now[0] += syncnow.TEAMS_BOOST_EVERY + 1
    fast.start()
    assert len(started) == 4 and fast.state()["boost"] == 0


def test_a_boost_after_a_channel_post_reads_that_channel(tmp_path):
    started = []
    now = [1000.0]
    fast = syncnow.TeamsFast(tmp_path, runner=lambda cmd, **kw: started.append(cmd) or _Done(), clock=lambda: now[0])
    fast.boost(("T1", "CH1"))
    fast.start()
    now[0] += syncnow.TEAMS_BOOST_FOR + 1
    fast.start()
    assert [c[-2:] for c in started] == [["--channel", "T1/CH1"], ["--recent", str(syncnow.TEAMS_RECENT)]]
