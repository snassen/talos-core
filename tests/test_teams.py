"""Teams sync against a fake Graph. The fake fails on anything but GET, and all data is invented."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import mailfactory as mf
import pytest

from talos import accounts, db, teams_ingest
from talos.ingest import Location
from talos.sources import base
from talos.sources.graph import GraphClient, GraphError
from talos.sources.teams import TeamsSource

G = "https://graph.microsoft.com/v1.0"
T0 = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)

ME = {"id": "u-me", "userId": "u-me", "displayName": "Alex Lind", "email": "Owner@Company.example"}
ANNA = {"id": "u-anna", "userId": "u-anna", "displayName": "Anna Berg", "email": "anna@kundbolaget.se"}
BO = {"id": "u-bo", "userId": "u-bo", "displayName": "Bo Ek", "email": "bo@company.example"}


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def msg(mid, who, text, at, *, mtype="message", subject=None, reply_to=None, attachments=(), edited=None):
    return {"id": mid, "messageType": mtype, "createdDateTime": iso(at),
            "lastModifiedDateTime": iso(edited or at), "lastEditedDateTime": iso(edited) if edited else None,
            "deletedDateTime": None, "subject": subject, "replyToId": reply_to, "importance": "normal",
            "from": {"user": {"id": who["id"], "displayName": who["displayName"],
                              "userIdentityType": "aadUser"}} if who else None,
            "body": {"contentType": "html", "content": f"<div><p>{text}</p></div>"},
            "attachments": list(attachments), "mentions": [], "reactions": []}


class FakeTeams:
    """Just enough of Graph's Teams API. Pages are small so paging is exercised."""

    def __init__(self, page=2):
        self.page = page
        self.requests: list[str] = []
        self.fail: dict[tuple, int] = {}  # (url fragments …) → status to answer with
        self.chats = {
            "C1": {"chat": {"id": "C1", "topic": "Projekt Norrsken", "chatType": "group"},
                   "members": [ME, ANNA, BO],
                   "messages": [msg(f"m{i}", [ANNA, BO, ME][i % 3], f"Meddelande nummer {i}", T0 + timedelta(minutes=i))
                                for i in range(1, 6)]
                   + [msg("sys1", None, "", T0, mtype="systemEventMessage")]},
            "C2": {"chat": {"id": "C2", "topic": None, "chatType": "oneOnOne"}, "members": [ME, ANNA],
                   "messages": [msg("d1", ANNA, "Hej! Här är offerten", T0, attachments=[
                       {"id": "f1", "contentType": "reference", "name": "Offert Norrsken.pdf",
                        "contentUrl": "https://kundbolaget.sharepoint.com/sites/x/Offert%20Norrsken.pdf"}])]},
            "C3": {"chat": {"id": "C3", "topic": "Gammal grupp", "chatType": "group"}, "members": [], "messages": [],
                   "forbidden": True},
        }
        self.teams = [{"id": "T1", "displayName": "Kundbolaget"}]
        self.channels = {"T1": [{"id": "CH1", "displayName": "Allmänt"}]}
        self.roots = {("T1", "CH1"): [
            {**msg("R1", ANNA, "Vi släpper på fredag", T0, subject="Release 2.0"),
             "replies": [msg("R1a", BO, "Låter bra", T0 + timedelta(minutes=5), reply_to="R1"),
                         msg("R1b", ME, "Jag kör tester", T0 + timedelta(minutes=6), reply_to="R1")]},
            {**msg("R2", BO, "Någon som vet om larmet?", T0 + timedelta(hours=1)),
             "replies": [msg("R2a", ANNA, "Det var backupen", T0 + timedelta(hours=1, minutes=1), reply_to="R2")],
             "replies@odata.nextLink": f"{G}/teams/T1/channels/CH1/messages/R2/replies?$skiptoken=x"},
        ]}
        self.all_replies = {"R2": [msg("R2a", ANNA, "Det var backupen", T0 + timedelta(hours=1, minutes=1),
                                       reply_to="R2"),
                                   msg("R2b", ME, "Tack!", T0 + timedelta(hours=1, minutes=2), reply_to="R2")]}
        self.delta: list[dict] = []

    def _paged(self, request, items):
        q = parse_qs(urlsplit(str(request.url)).query)
        skip = int(q.get("$skiptoken", ["0"])[0])
        body = {"value": items[skip:skip + self.page]}
        if skip + self.page < len(items):
            nxt = httpx.URL(str(request.url)).copy_set_param("$skiptoken", str(skip + self.page))
            body["@odata.nextLink"] = str(nxt)
        return httpx.Response(200, json=body)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "Talos must only ever GET from Graph"
        url = str(request.url)
        self.requests.append(url)
        path = request.url.path.removeprefix("/v1.0")
        q = parse_qs(urlsplit(url).query)
        for frags, status in self.fail.items():
            if all(f in unquote(url) for f in frags):
                return httpx.Response(status, text="fake failure")
        if path == "/me":
            return httpx.Response(200, json={"id": "u-me", "mail": "owner@company.example",
                                             "userPrincipalName": "o@company.example"})
        if path == "/me/chats":
            out = []
            for c in self.chats.values():
                newest = max((m for m in c["messages"] if m["messageType"] == "message"),
                             key=lambda m: m["createdDateTime"], default=None)
                out.append({**c["chat"], "lastMessagePreview": {"id": newest["id"], "createdDateTime": newest["createdDateTime"]}
                            if newest else None})
            return self._paged(request, out)
        parts = path.strip("/").split("/")
        if parts[0] == "chats":
            chat = self.chats[parts[1]]
            if chat.get("forbidden"):
                return httpx.Response(403, json={"error": {"code": "Forbidden"}})
            if parts[2] == "members":
                return self._paged(request, [{**m, "@odata.type": "#microsoft.graph.aadUserConversationMember"}
                                             for m in chat["members"]])
            items = sorted(chat["messages"], key=lambda m: m["lastModifiedDateTime"], reverse=True)
            if "$filter" in q:
                flt = q["$filter"][0]
                assert q.get("$orderby") == ["lastModifiedDateTime desc"]
                since = flt.split("lastModifiedDateTime gt ")[1].split(" ")[0]
                items = [m for m in items if m["lastModifiedDateTime"] > since]
            return self._paged(request, items)
        if path == "/me/joinedTeams":
            return self._paged(request, self.teams)
        if parts[0] == "teams" and len(parts) == 3:
            return self._paged(request, self.channels[parts[1]])
        if parts[0] == "teams" and parts[-1] == "delta":
            return httpx.Response(200, json={"value": self.delta, "@odata.deltaLink": f"{G}/deltaLink/CH1?n={len(self.requests)}"})
        if parts[0] == "deltaLink":
            out, self.delta = self.delta, []
            return httpx.Response(200, json={"value": out, "@odata.deltaLink": f"{G}/deltaLink/CH1?n={len(self.requests)}"})
        if parts[0] == "teams" and parts[-1] == "replies":
            return self._paged(request, self.all_replies.get(parts[-2], []))
        if parts[0] == "teams" and parts[-1] == "messages":
            assert q.get("$expand") == ["replies"]
            return self._paged(request, self.roots[(parts[1], parts[3])])
        return httpx.Response(404, text=f"unexpected {url}")


class Clock:
    def __init__(self):
        self.now = T0 + timedelta(days=1)

    def __call__(self):
        return self.now


@pytest.fixture
def teams_conn(conn):
    conn.execute("truncate teams_user")
    conn.execute("insert into account (id, provider, address, enabled) values"
                 " ('teams', 'teams', 'owner@company.example', true)")
    conn.commit()
    return conn


def source(fake, clock=None, slept=None, **kw):
    client = GraphClient(lambda: "token", httpx.Client(transport=httpx.MockTransport(fake)),
                         sleep=(slept.append if slept is not None else lambda s: None))
    return TeamsSource(client, clock=clock or Clock(), **kw)


def run(conn, ingestor, src, **kw):
    return base.run(conn, ingestor, "teams", src, **kw)


def rows(conn, sql, *args):
    return conn.execute(sql, args or None).fetchall()


# ---------------------------------------------------------------------------- chats


def test_a_chat_backfill_across_several_pages_resumes_after_a_crash_midway(teams_conn, ingestor):
    fake = FakeTeams(page=2)
    fake.fail = {("/chats/C1/messages", "skiptoken=4"): 500}
    with pytest.raises(GraphError):
        run(teams_conn, ingestor, source(fake, channels=False))
    teams_conn.commit()
    got = {r["provider_key"] for r in rows(teams_conn, "select provider_key from message")}
    assert got == {"chat:C1:m5", "chat:C1:m4", "chat:C1:m3", "chat:C1:m2"}  # the two committed pages
    state = teams_conn.execute("select state from sync_cursor where scope = 'chat:C1'").fetchone()["state"]
    assert "skiptoken=4" in state["next_link"] and not state.get("backfilled")

    assert "still failing" in state["error"]  # the failure is written down with the chat
    assert state["error_status"] is None and state["error_at"]

    fake.fail = {}
    fake.requests.clear()
    clock = Clock()
    run(teams_conn, ingestor, source(fake, clock, channels=False))  # within the hour: C1 waits, C2 goes on
    assert not any("/chats/C1/" in u for u in fake.requests)
    fake.requests.clear()
    clock.now += timedelta(hours=2)
    stats = run(teams_conn, ingestor, source(fake, clock, channels=False))
    c1 = [u for u in fake.requests if "/chats/C1/messages" in u]
    assert c1 and all("skiptoken" in u for u in c1)  # resumed; the first pages were not fetched again
    assert stats.added == 1  # m1; C2's d1 came in the run before
    assert teams_conn.execute("select count(*) n from message where provider_key like 'chat:C1:%'").fetchone()["n"] == 5
    state = teams_conn.execute("select state from sync_cursor where scope = 'chat:C1'").fetchone()["state"]
    assert state["backfilled"] and state["next_link"] is None and state["since"] == iso(T0 + timedelta(minutes=5))


def test_a_chat_becomes_one_thread_with_its_topic_and_direction_from_the_sender(teams_conn, ingestor):
    run(teams_conn, ingestor, source(FakeTeams(), channels=False))
    msgs = rows(teams_conn, "select m.provider_key, m.medium, m.direction, m.subject, m.from_address, t.provider_thread_id,"
                            " t.subject tsubj, t.message_count, x.body_text from message m join thread t on t.id = m.thread_id"
                            " join message_text x on x.message_id = m.id where m.provider_key like 'chat:C1:%'")
    assert {r["provider_thread_id"] for r in msgs} == {"chat:C1"}
    assert {r["tsubj"] for r in msgs} == {"Projekt Norrsken"} and msgs[0]["message_count"] == 5
    assert all(r["medium"] == "teams_chat" and r["subject"] == "Projekt Norrsken" for r in msgs)
    by = {r["provider_key"]: r for r in msgs}
    assert by["chat:C1:m2"]["direction"] == "out" and by["chat:C1:m2"]["from_address"] == "owner@company.example"
    assert by["chat:C1:m1"]["direction"] == "in" and by["chat:C1:m1"]["body_text"] == "Meddelande nummer 1"
    one_on_one = teams_conn.execute("select subject from thread where provider_thread_id = 'chat:C2'").fetchone()
    assert one_on_one["subject"] == "Anna Berg"  # no topic: named after the other person
    hits = teams_conn.execute("select count(*) n from message_text where search @@ websearch_to_tsquery('swedish', 'meddelanden')").fetchone()
    assert hits["n"] == 5  # the Swedish + English search vector is built as for mail


def test_system_messages_are_skipped(teams_conn, ingestor):
    stats = run(teams_conn, ingestor, source(FakeTeams(), channels=False))
    assert teams_conn.execute("select count(*) n from message where provider_key like '%sys1'").fetchone()["n"] == 0
    assert stats.added == 6


def test_a_teams_sender_and_an_email_sender_with_the_same_address_are_one_person(teams_conn, ingestor):
    ingestor.ingest("work", mf.make(frm="Anna Berg <anna@kundbolaget.se>", to="owner@company.example",
                                   subject="Offert"), Location("Inkorgen", "mail-1"))
    run(teams_conn, ingestor, source(FakeTeams(), channels=False))
    people = rows(teams_conn, "select person_id from address where address = 'anna@kundbolaget.se'")
    assert len(people) == 1
    pid = people[0]["person_id"]
    senders = rows(teams_conn, "select m.medium from edge e join message m on m.id = e.src"
                               " where e.rel = 'from' and e.dst = %s", pid)
    assert {r["medium"] for r in senders} == {"email", "teams_chat"}
    org = teams_conn.execute("select o.domain from person p join org o on o.id = p.org_id where p.id = %s", (pid,)).fetchone()
    assert org["domain"] == "kundbolaget.se"
    # the chat's other members are recipients, as on a mail
    to = rows(teams_conn, "select p.address from participant p join message m on m.id = p.message_id"
                          " where m.provider_key = 'chat:C1:m1' and p.role = 'to'")
    assert {r["address"] for r in to} == {"anna@kundbolaget.se", "owner@company.example"}  # m1 is Bo's


def test_incremental_sync_picks_up_only_new_or_edited_messages(teams_conn, ingestor):
    fake, clock = FakeTeams(), Clock()
    run(teams_conn, ingestor, source(fake, clock, channels=False))
    edited_at = T0 + timedelta(days=1, minutes=1)
    c1 = fake.chats["C1"]["messages"]
    c1[1] = msg("m2", BO, "Meddelande nummer 2, rättat", T0 + timedelta(minutes=2), edited=edited_at)
    c1.append(msg("m6", ANNA, "Ett nytt meddelande", T0 + timedelta(days=1, minutes=2)))
    fake.requests.clear()
    clock.now += timedelta(minutes=5)
    stats = run(teams_conn, ingestor, source(fake, clock, channels=False))
    assert (stats.added, stats.updated) == (1, 1)
    asked = [u for u in fake.requests if "/messages" in u]
    assert all("filter" in u for u in asked if "/chats/C1/" in u)
    assert not any("/chats/C2/" in u for u in asked)  # nothing new there, and checked five minutes ago
    text = teams_conn.execute("select x.body_text from message m join message_text x on x.message_id = m.id"
                              " where m.provider_key = 'chat:C1:m2'").fetchone()["body_text"]
    assert text == "Meddelande nummer 2, rättat"
    versions = teams_conn.execute("select count(*) n from message_original o join message m on m.id = o.message_id"
                                  " where m.provider_key = 'chat:C1:m2'").fetchone()["n"]
    assert versions == 2  # the edit is the row; the earlier JSON is kept too
    # a third run with nothing new asks no chat for messages at all
    fake.requests.clear()
    assert run(teams_conn, ingestor, source(fake, clock, channels=False)).added == 0
    assert not any("/messages" in u for u in fake.requests)
    # once RECHECK has passed a quiet chat is asked again, for edits and deletions
    c1[0] = {**c1[0], "deletedDateTime": iso(clock.now), "lastModifiedDateTime": iso(clock.now),
             "body": {"contentType": "html", "content": ""}}
    clock.now += timedelta(hours=7)
    assert run(teams_conn, ingestor, source(fake, clock, channels=False)).updated == 1
    gone = teams_conn.execute("select l.present, l.flags, x.body_text from message m join message_location l on"
                              " l.message_id = m.id join message_text x on x.message_id = m.id"
                              " where m.provider_key = 'chat:C1:m1'").fetchone()
    assert gone["present"] is False and "deleted" in gone["flags"]
    assert gone["body_text"] == "Meddelande nummer 1"  # what was said before the deletion is kept


def test_a_forbidden_chat_is_skipped_without_failing_the_run(teams_conn, ingestor):
    fake, clock = FakeTeams(), Clock()
    stats = run(teams_conn, ingestor, source(fake, clock, channels=False))
    assert stats.added == 6 and any("Gammal grupp" in n and "403" in n for n in stats.notes)
    assert teams_conn.execute("select status from sync_run order by id desc limit 1").fetchone()["status"] == "partial"
    state = teams_conn.execute("select state from sync_cursor where scope = 'chat:C3'").fetchone()["state"]
    assert "403" in state["error"]
    fake.requests.clear()
    run(teams_conn, ingestor, source(fake, clock, channels=False))
    assert not any("/chats/C3/" in u for u in fake.requests)  # not asked again within a day


def test_reference_attachments_are_stored_as_metadata_only(teams_conn, ingestor, vault, database):
    run(teams_conn, ingestor, source(FakeTeams(), channels=False))
    att = teams_conn.execute("select a.*, m.has_attachments from attachment a join message m on m.id = a.message_id").fetchone()
    assert att["blob_sha256"] is None and att["size_bytes"] is None and att["has_attachments"]
    assert att["filename"] == "Offert Norrsken.pdf" and att["content_type"] == "application/pdf"
    assert att["attrs"]["url"].startswith("https://kundbolaget.sharepoint.com/") and att["attrs"]["reference"]
    assert not (vault.root / "att").exists()  # nothing was downloaded
    assert teams_conn.execute("select count(*) n from edge where rel = 'has_attachment'").fetchone()["n"] == 1

    teams_conn.commit()
    from test_web import client
    c = client(database, vault)
    assert c.get(f"/api/attachments/{att['id']}").status_code == 404
    original = c.get(f"/api/messages/{att['message_id']}/raw")
    assert original.headers["content-type"].startswith("application/json")
    assert json.loads(original.content)["id"] == "d1"
    listed = c.get("/api/messages", params={"q": "offerten"}).json()["rows"]
    assert listed[0]["medium"] == "teams_chat"


def test_each_original_is_canonical_json_in_the_vault(teams_conn, ingestor, vault):
    fake = FakeTeams()
    run(teams_conn, ingestor, source(fake, channels=False))
    row = teams_conn.execute("select m.raw_sha256, b.kind, b.path from message m join blob b on b.sha256 = m.raw_sha256"
                             " where m.provider_key = 'chat:C1:m1'").fetchone()
    assert row["kind"] == "json" and row["path"].endswith(".json.zst")
    stored = vault.get(row["raw_sha256"], "json")
    assert stored == teams_ingest.canonical(json.loads(stored))
    assert json.loads(stored)["body"]["content"] == "<div><p>Meddelande nummer 1</p></div>"


def test_only_get_is_ever_sent(teams_conn, ingestor):
    fake = FakeTeams()
    run(teams_conn, ingestor, source(fake))  # the fake asserts the method of every request
    assert fake.requests
    with pytest.raises(AssertionError):
        fake(httpx.Request("POST", f"{G}/chats/C1/messages"))


def test_the_limit_stops_a_backfill_in_slices(teams_conn, ingestor):
    fake = FakeTeams()
    s1 = run(teams_conn, ingestor, source(fake, channels=False), limit=3)
    assert s1.added <= 4 and any("limit" in n for n in s1.notes)
    total = s1.added
    for _ in range(5):
        total += run(teams_conn, ingestor, source(fake, channels=False), limit=3).added
    assert total == 6 and teams_conn.execute("select count(*) n from message").fetchone()["n"] == 6


# ---------------------------------------------------------------------------- channels


def test_channel_messages_and_replies_thread_by_reply_chain(teams_conn, ingestor):
    fake, clock = FakeTeams(), Clock()
    run(teams_conn, ingestor, source(fake, clock))
    got = rows(teams_conn, "select m.provider_key, m.medium, m.subject, m.direction, t.provider_thread_id, t.subject tsubj,"
                           " l.folder from message m join thread t on t.id = m.thread_id"
                           " join message_location l on l.message_id = m.id where m.medium = 'teams_channel'")
    by = {r["provider_key"].rsplit(":", 1)[1]: r for r in got}
    assert set(by) == {"R1", "R1a", "R1b", "R2", "R2a", "R2b"}  # R2's cut-off replies were fetched in full
    assert {by[k]["provider_thread_id"] for k in ("R1", "R1a", "R1b")} == {"channel:T1:CH1:R1"}
    assert {by[k]["provider_thread_id"] for k in ("R2", "R2a", "R2b")} == {"channel:T1:CH1:R2"}
    assert by["R1a"]["tsubj"] == "Release 2.0" and by["R1a"]["subject"] == "Release 2.0"
    assert by["R2"]["tsubj"] == "Kundbolaget › Allmänt"  # no subject: the channel names the thread
    assert by["R1b"]["direction"] == "out" and by["R1a"]["folder"] == "Teams/Kundbolaget/Allmänt"
    # Bo's address was learnt from a chat, so his channel posts have a sender too
    assert by["R2"]["provider_key"] == "channel:T1:CH1:R2"
    assert teams_conn.execute("select from_address from message where provider_key = 'channel:T1:CH1:R1a'"
                              ).fetchone()["from_address"] == "bo@company.example"

    # incremental: the delta brings a new post and a reply listed on its own
    fake.delta = [msg("R3", ANNA, "Ny tråd", T0 + timedelta(days=1)),
                  msg("R1c", ANNA, "Klart!", T0 + timedelta(days=1, minutes=1), reply_to="R1")]
    stats = run(teams_conn, ingestor, source(fake, clock))
    assert stats.added == 2
    r1c = teams_conn.execute("select t.provider_thread_id from message m join thread t on t.id = m.thread_id"
                             " where m.provider_key = 'channel:T1:CH1:R1c'").fetchone()
    assert r1c["provider_thread_id"] == "channel:T1:CH1:R1"
    state = teams_conn.execute("select state from sync_cursor where scope = 'channel:T1:CH1'").fetchone()["state"]
    assert state["backfilled"] and state["delta_link"].startswith(f"{G}/deltaLink/")
    first_delta = [u for u in fake.requests if u.split("?")[0].endswith("/messages/delta")]
    assert len(first_delta) == 1 and "lastModifiedDateTime" in first_delta[0]


def test_channels_are_skipped_with_a_note_when_teams_cannot_be_listed(teams_conn, ingestor):
    fake = FakeTeams()
    fake.fail = {("/me/joinedTeams",): 403}
    stats = run(teams_conn, ingestor, source(fake))
    assert stats.added == 6 and any("joined teams" in n for n in stats.notes)
    # a channel named in the account settings is still read
    stats = run(teams_conn, ingestor, source(fake, extra_channels=[
        {"team_id": "T1", "channel_id": "CH1", "team_name": "Kundbolaget", "channel_name": "Allmänt"}]))
    assert stats.added == 6


# ---------------------------------------------------------------------------- account, schema


def test_the_teams_account_is_seeded_disabled_and_reseeding_never_switches_it(conn):
    conn.execute("delete from account where id = 'teams'")
    accounts.seed(conn)
    assert conn.execute("select enabled from account where id = 'teams'").fetchone()["enabled"] is False
    settings = conn.execute("select settings from account where id = 'teams'").fetchone()["settings"]
    assert settings["token_account"] == "work" and settings["secret"] == "graph-token-cache:work"
    accounts.seed(conn)
    assert conn.execute("select enabled from account where id = 'teams'").fetchone()["enabled"] is False
    conn.execute("update account set enabled = true where id = 'teams'")
    accounts.seed(conn)
    assert conn.execute("select enabled from account where id = 'teams'").fetchone()["enabled"] is True


def test_a_plain_talos_sync_does_not_select_the_teams_account_while_it_is_disabled(conn, database, tmp_path, monkeypatch):
    from talos import cli, secrets
    accounts.seed(conn)
    conn.commit()
    picked = []

    class Nothing:
        def sync(self, ctx):
            return base.SyncStats()

    monkeypatch.setenv("TALOS_DSN", database)
    monkeypatch.setenv("TALOS_HOME", str(tmp_path))
    monkeypatch.setattr(secrets, "exists", lambda key: True)  # never touch the Keychain in tests
    monkeypatch.setattr(accounts, "source_for", lambda acct: picked.append(acct["id"]) or Nothing())
    cli.main(["sync"])
    assert "teams" not in picked and "work" in picked
    cli.main(["sync", "teams"])
    assert picked.count("teams") == 0
    conn.execute("update account set enabled = true where id = 'teams'")
    conn.commit()
    cli.main(["sync", "teams"])
    assert picked[-1] == "teams"


def test_the_teams_source_is_built_on_the_work_accounts_sign_in_and_names_it(monkeypatch):
    from talos import graphauth
    seen = []

    class FakeAuth:
        def __init__(self, account_id, tenant_id, client_id):
            seen.append(account_id)

        def token(self):
            return "t"

    monkeypatch.setattr(graphauth, "GraphAuth", FakeAuth)
    teams = next(a for a in accounts.ACCOUNTS if a["id"] == "teams")
    assert isinstance(accounts.source_for(teams), TeamsSource) and seen == ["work"]
    # no account is assumed: the sign-in is the one token_account names
    unnamed = {**teams, "settings": {k: v for k, v in teams["settings"].items() if k != "token_account"}}
    with pytest.raises(KeyError, match="token_account"):
        accounts.source_for(unnamed)


def test_migration_005_widens_checks_on_a_populated_database_and_keeps_other_values(database):
    names = dict(db.migrations())
    with db.connect(database) as c:
        c.execute("drop schema if exists mig005 cascade")
        c.execute("create schema mig005")
        c.execute("set search_path = mig005, public")
        c.execute(names["001_foundation.sql"])
        c.execute(names["002_ingest_failure.sql"])
        # data already there, and a provider that another migration might have added
        c.execute("alter table account drop constraint account_provider_check")
        c.execute("alter table account add constraint account_provider_check"
                  " check (provider in ('gmail', 'graph', 'imap', 'local', 'caldav'))")
        c.execute("insert into account (id, provider, address) values ('gmail', 'gmail', 'x@y.se')")
        c.execute("insert into blob (sha256, kind, size, stored_size, codec, path) values ('ab', 'attachment', 1, 1, 'none', 'p')")
        c.execute("insert into entity (id, kind) values (1, 'message'), (2, 'attachment')")
        c.execute("insert into message (id, account_id, provider_key, direction, parser_version) values (1, 'gmail', 'k', 'in', 1)")
        c.execute("insert into attachment (id, message_id, blob_sha256, part_path, content_type, size_bytes)"
                  " values (2, 1, 'ab', '2', 'application/pdf', 1)")
        c.execute(names["005_teams.sql"])
        c.execute("insert into account (id, provider, address) values ('teams', 'teams', 'x@y.se'), ('cal', 'caldav', 'x@y.se')")
        c.execute("insert into blob (sha256, kind, size, stored_size, codec, path) values ('cd', 'json', 1, 1, 'zstd', 'q')")
        with pytest.raises(Exception):
            with c.transaction():
                c.execute("insert into account (id, provider, address) values ('bad', 'nope', 'x')")
        with pytest.raises(Exception):  # a link needs its url
            with c.transaction():
                c.execute("insert into attachment (id, message_id, part_path, content_type) values (1, 1, '3', 'x')")
        c.rollback()
        c.execute("drop schema if exists mig005 cascade")
        c.commit()


def test_a_changeset_never_plans_a_change_to_a_teams_message(teams_conn, ingestor):
    from talos import changesets
    ingestor.ingest("work", mf.make(frm="Anna Berg <anna@kundbolaget.se>", to="owner@company.example"),
                    Location("Inkorgen", "mail-1"))
    run(teams_conn, ingestor, source(FakeTeams(), channels=False))
    ids = [r["id"] for r in rows(teams_conn, "select id from message where from_address = 'anna@kundbolaget.se'")]
    cid = changesets.create(teams_conn, "Läst", "mark_read", message_ids=ids)
    result = changesets.plan(teams_conn, cid)
    assert result["by_account"] == {"work:pending": 1, "teams:skipped": len(ids) - 1}
    assert result["skipped_because"] == {"a Teams message; Talos never changes Teams": len(ids) - 1}
