"""Importance: the deterministic score, the owner's override, recompute, and the API. Invented mail only."""

from datetime import datetime, timedelta, timezone

import mailfactory as mf
from starlette.testclient import TestClient

from talos import importance, rules
from talos.config import Settings
from talos.ingest import Location
from talos.web import app

ME = mf.ME
OSKAR = "Oskar Nyström <oskar@nordvik.se>"
NOW = datetime.now(timezone.utc)
_keys = iter(range(1, 10_000))


def ago(**kw) -> datetime:
    return NOW - timedelta(**kw)


def mail(ingestor, **kw) -> int:
    n = next(_keys)
    kw.setdefault("msgid", f"<m{n}@test.invalid>")
    kw.setdefault("date", ago(hours=2))
    return ingestor.ingest("gmail", mf.make(**kw), Location("[all]", f"k{n}")).message_id


def colleague_he_answers(ingestor, n=6):
    """The owner has written to Oskar n times this year, in threads of their own."""
    for i in range(n):
        mail(ingestor, frm=ME, to=OSKAR, subject=f"Svar {i}", body="Tack, det ordnar jag.", date=ago(days=40 + i))


def question(ingestor, **kw) -> int:
    kw = {"frm": OSKAR, "to": ME, "subject": "Offerten", "msgid": "<fraga@nordvik.se>",
          "body": "Hej Alex,\n\nKan du titta på offerten innan mötet?\n\nMvh Oskar", "date": ago(days=3), **kw}
    return mail(ingestor, **kw)


def reply(ingestor, to_msgid="<fraga@nordvik.se>", **kw) -> int:
    kw = {"frm": ME, "to": OSKAR, "subject": "Re: Offerten", "body": "Absolut, jag tittar i eftermiddag.",
          "in_reply_to": to_msgid, "references": to_msgid, "date": ago(days=1), **kw}
    return mail(ingestor, **kw)


def imp(conn, mid) -> dict:
    return conn.execute("select * from message_importance where message_id = %s", (mid,)).fetchone()


def signals(row) -> set[str]:
    return {r["signal"] for r in row["reasons"]}


def client(database, vault):
    return TestClient(app.create(Settings(home=vault.root.parent, dsn=database), allowed_hosts=["testserver"]))


# ---------------------------------------------------------------- the score

def test_a_direct_question_from_a_colleague_the_owner_often_answers_ranks_high(conn, ingestor):
    colleague_he_answers(ingestor)
    mid = question(ingestor)
    importance.compute(conn)
    row = imp(conn, mid)
    assert row["level"] == "high" and row["waiting"] is True
    assert {"person", "direct", "replied_often", "question"} <= signals(row)
    assert row["score"] == sum(r["points"] for r in row["reasons"])
    assert row["version"] == importance.VERSION


def test_the_same_message_after_the_owners_reply_is_no_longer_waiting(conn, ingestor):
    colleague_he_answers(ingestor)
    mid = question(ingestor)
    reply(ingestor)
    importance.compute(conn)
    row = imp(conn, mid)
    assert row["waiting"] is False
    assert "question" not in signals(row) and {"answered", "in_thread"} <= signals(row)
    assert row["level"] == "high"  # still important, just answered


def test_a_newsletter_ranks_as_noise_even_when_it_is_sent_to_him(conn, ingestor):
    mid = mail(ingestor, frm="Nordvik <nyheter@nordvik.se>", to=ME, subject="Nyhetsbrev: kan du missa detta?",
               headers={"List-Id": "<nyheter.nordvik.se>", "List-Unsubscribe": "<https://nordvik.se/av>"})
    importance.compute(conn)
    row = imp(conn, mid)
    assert row["level"] == "noise" and row["waiting"] is False
    assert {"list", "automated"} <= signals(row) and "direct" not in signals(row)


def test_a_message_the_owner_filed_as_a_newsletter_is_noise_though_a_person_sent_it(conn, ingestor):
    mid = mail(ingestor, frm=OSKAR, to=ME, subject="Veckans tips", body="Här är veckans tips.")
    rules.assign(conn, [mid], "type", "newsletter")
    importance.compute(conn)
    row = imp(conn, mid)
    assert row["level"] == "noise" and "filed_noise" in signals(row)


def test_cc_only_ranks_below_to(conn, ingestor):
    to = mail(ingestor, frm=OSKAR, to=ME, subject="Status", body="Här är statusen.")
    cc = mail(ingestor, frm=OSKAR, to="Mikael <mikael@nordvik.se>", cc=ME, subject="Status 2", body="Här är statusen.")
    importance.compute(conn)
    assert imp(conn, cc)["score"] < imp(conn, to)["score"]
    assert "copied" in signals(imp(conn, cc)) and "direct" in signals(imp(conn, to))


def test_a_question_in_a_link_is_not_a_question(conn, ingestor):
    mid = mail(ingestor, frm=OSKAR, to=ME, subject="Länk", body="Se https://nordvik.se/sida?id=4 för detaljer.")
    importance.compute(conn)
    assert imp(conn, mid)["waiting"] is False


def test_a_deadline_or_a_date_in_the_coming_days_adds_a_little(conn, ingestor):
    soon = (NOW + timedelta(days=4)).date().isoformat()
    a = mail(ingestor, frm=OSKAR, to=ME, subject="Rapport", body="Rapporten behövs senast fredag.")
    b = mail(ingestor, frm=OSKAR, to=ME, subject="Rapport 2", body=f"Leverans {soon}.")
    c = mail(ingestor, frm=OSKAR, to=ME, subject="Rapport 3", body="Leverans 2019-02-31 eller 2019-01-01.")
    importance.compute(conn)
    assert "deadline" in signals(imp(conn, a)) and "deadline" in signals(imp(conn, b))
    assert "deadline" not in signals(imp(conn, c))


def test_his_not_important_mark_beats_every_signal(conn, ingestor):
    colleague_he_answers(ingestor)
    mid = question(ingestor)
    importance.vip_add(conn, "oskar@nordvik.se")
    res = importance.mark(conn, mid, "not_important")
    assert res["level"] == "noise" and res["waiting"] is False and res["marked"] == "not_important"
    assert any(r["signal"] == "marked" and r["text"] == "You marked it not important" for r in res["reasons"])
    importance.compute(conn)  # a full run keeps the owner's mark
    assert imp(conn, mid)["level"] == "noise"
    assert importance.mark(conn, mid, None)["level"] == "high"


def test_his_important_mark_forces_high(conn, ingestor):
    mid = mail(ingestor, frm="Nordvik <nyheter@nordvik.se>", to=ME, headers={"List-Id": "<n.nordvik.se>"})
    assert importance.mark(conn, mid, "important")["level"] == "high"


def test_a_vip_sender_ranks_high(conn, ingestor):
    mid = mail(ingestor, frm="Petra Lind <petra@styrelsen.se>", to=ME, subject="Hej", body="Ett kort besked.")
    importance.compute(conn)
    assert imp(conn, mid)["level"] == "low"
    importance.vip_add(conn, "styrelsen.se", "the board")
    row = imp(conn, mid)
    assert row["level"] == "high" and "vip" in signals(row)
    importance.vip_remove(conn, "styrelsen.se")
    assert imp(conn, mid)["level"] == "low"


def test_a_small_teams_chat_counts_like_direct_mail(conn, ingestor):
    colleague_he_answers(ingestor, n=1)
    mid = mail(ingestor, frm=OSKAR, to=ME, subject="Chatt", body="Hinner du ringa?")
    conn.execute("update message set medium = 'teams_chat' where id = %s", (mid,))
    importance.compute(conn)
    row = imp(conn, mid)
    assert "direct" in signals(row) and row["waiting"] is True
    assert any(r["text"] == "A chat with you and few others" for r in row["reasons"])


def test_his_own_messages_are_low_and_say_so(conn, ingestor):
    mid = reply(ingestor)
    importance.compute(conn)
    row = imp(conn, mid)
    assert row["level"] == "low" and row["score"] == 0 and signals(row) == {"own"}


# ---------------------------------------------------------------- recompute

def test_incremental_recompute_picks_up_a_new_reply(conn, ingestor):
    colleague_he_answers(ingestor)
    mid = question(ingestor, date=ago(days=10))
    importance.compute(conn)
    assert imp(conn, mid)["waiting"] is True
    reply(ingestor, date=ago(hours=1))
    res = importance.compute(conn, since=ago(days=1))
    assert res["scored"] >= 2  # the reply, and the older question in the thread it touched
    assert imp(conn, mid)["waiting"] is False


def test_an_unchanged_message_is_not_rewritten(conn, ingestor):
    question(ingestor)
    importance.compute(conn)
    assert importance.compute(conn)["changed"] == 0


# ---------------------------------------------------------------- reading

def test_today_waiting_and_check_lists(conn, ingestor):
    colleague_he_answers(ingestor)
    old = question(ingestor, date=ago(days=4))
    new = mail(ingestor, frm=OSKAR, to=ME, subject="Snabbt", body="Kan du svara?", date=ago(hours=1))
    importance.compute(conn)
    top = importance.today(conn, limit=5)
    assert [r["id"] for r in top] == [new] and top[0]["waiting"] is True and top[0]["reasons"]
    waits = importance.waiting(conn)
    assert [r["id"] for r in waits] == [old, new] and waits[0]["waited_days"] >= 4
    check = importance.check(conn)
    assert check[0]["id"] == old and check[0]["kind"] == "waiting"
    assert check[0]["reason"].startswith("Asked you something 4 days ago")


def test_the_api_serves_importance_and_the_owners_mark_needs_the_header(conn, ingestor, vault, database):
    colleague_he_answers(ingestor)
    mid = question(ingestor, date=ago(hours=3))
    low = mail(ingestor, frm="Petra <petra@exempel.se>", to=ME, subject="Hej", body="Hej.")
    importance.compute(conn)
    conn.commit()
    c = client(database, vault)
    assert c.post(f"/api/messages/{mid}/importance", json={"value": "not_important"}).status_code == 403
    assert c.get(f"/api/messages/{mid}").json()["importance"]["level"] == "high"
    r = c.post(f"/api/messages/{mid}/importance", json={"value": "not_important"}, headers={"X-Talos": "1"})
    assert r.status_code == 200 and r.json()["level"] == "noise" and r.json()["marked"] == "not_important"
    assert c.post(f"/api/messages/{mid}/importance", json={"value": "maybe"}, headers={"X-Talos": "1"}).status_code == 400
    assert c.post("/api/messages/999999/importance", json={"value": None}, headers={"X-Talos": "1"}).status_code == 404
    r = c.post(f"/api/messages/{mid}/importance", json={"value": None}, headers={"X-Talos": "1"})
    assert r.json()["level"] == "high" and r.json()["marked"] is None

    rows = c.get("/api/messages", params={"direction": "in"}).json()["rows"]
    assert {r["id"]: r["importance"]["level"] for r in rows} == {mid: "high", low: "low"}
    assert [r["id"] for r in c.get("/api/messages", params={"important": "1"}).json()["rows"]] == [mid]
    assert c.get("/api/messages", params={"sort": "importance"}).json()["rows"][0]["id"] == mid
    assert [r["id"] for r in c.get("/api/importance/today").json()] == [mid]
    assert [r["id"] for r in c.get("/api/importance/waiting").json()] == [mid]
    assert c.get("/api/importance/daily").status_code == 404  # the per-day widget and its endpoint are gone
    assert c.get("/api/importance/check").status_code == 200


# ---------------------------------------------------------------- tuned on real mail (version 2)

def test_a_question_from_a_stranger_is_not_waiting_for_him(conn, ingestor):
    """Real mail: shop adverts and a quarantine notice ended up 'waiting' because they asked something."""
    mid = mail(ingestor, frm="Blocket <annonser@blocket.se>", to=ME, subject="Nya annonser: minidisc",
               body="Vill du se fler annonser?")
    importance.compute(conn)
    row = imp(conn, mid)
    assert row["waiting"] is False and "question" not in signals(row)


def test_a_colleague_in_the_owners_own_organisation_counts_as_known(conn, ingestor):
    mid = mail(ingestor, frm="Kollega <kollega@company.example>", to="owner@company.example",  # the owner's own domain
               subject="Budget", body="Har du en siffra före den 30:e?")
    importance.compute(conn)
    assert imp(conn, mid)["waiting"] is True


def test_calendar_replies_are_not_questions(conn, ingestor):
    colleague_he_answers(ingestor)
    for subject in ("Canceled: Säljmöte?", "Avböjd: Finops - Standup", "Accepterad: Möte?"):
        mid = mail(ingestor, frm=OSKAR, to=ME, subject=subject, body="Mötet. Frågor?")
        importance.compute(conn)
        row = imp(conn, mid)
        assert row["waiting"] is False and "meeting_reply" in signals(row), subject


def test_a_busy_conversation_shows_once_in_todays_list(conn, ingestor):
    colleague_he_answers(ingestor)
    for i in range(4):
        mail(ingestor, frm=OSKAR, to=ME, subject="Samma tråd", msgid=f"<t{i}@n>",
             in_reply_to="<t0@n>" if i else None, references="<t0@n>" if i else None,
             body=f"Kan du kolla punkt {i}?", date=ago(hours=5 - i))
    mail(ingestor, frm=OSKAR, to=ME, subject="Annan sak", body="Kan du ringa?", date=ago(hours=1))  # another thread
    importance.compute(conn)
    rows = importance.today(conn, limit=5)
    assert len(rows) == 2 and {r["subject"] for r in rows} == {"Samma tråd", "Annan sak"}
