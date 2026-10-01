"""Composing and sending: the confirmation, the sending accounts, signatures, MIME, the rate limit,
the send log, and a send end to end through a fake SMTP server. No test sends a real mail:
every transport here is the loopback fake (smtpfake.FakeSmtp) or an in-memory stand-in."""

from __future__ import annotations

import base64
import email
from email import policy

import httpx
import mailfactory as mf
import pytest
from psycopg.types.json import Jsonb
from smtpfake import FakeSmtp
from starlette.testclient import TestClient

from talos import compose, personal, send
from talos.config import Settings
from talos.ingest import Location
from talos.web import app

SAME = {"X-Talos": "1", "Sec-Fetch-Site": "same-origin"}


class FakeProbe(compose.Probe):
    def __init__(self, secrets=("gmail:owner@gmail.com",), graph=False):
        super().__init__()
        self.secrets, self.graph = set(secrets), graph

    def secret_exists(self, key):
        return key in self.secrets

    def graph_can_send(self, account):
        return self.graph


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Recorder:
    """A transport that keeps what it was given and delivers nothing."""

    def __init__(self):
        self.sent = []

    def deliver(self, msg, sender, recipients):
        self.sent.append((msg, sender, recipients))


@pytest.fixture
def cconn(conn):
    """The test database with the send tables empty and the accounts as seeded."""
    conn.execute("truncate send_log, send_setting, signature_default, draft, signature restart identity cascade")
    conn.execute("update account set settings = %s where id = 'gmail'",
                 (Jsonb({"secret": "gmail:owner@gmail.com"}),))
    conn.execute("update account set settings = %s where id = 'work'",
                 (Jsonb({"tenant_id": "t", "client_id": "c"}),))
    conn.execute("insert into account (id, provider, address, display_name, enabled, settings) values"
                 " ('icloud', 'imap', 'owner@icloud.example', 'iCloud', false,"
                 "  '{\"host\": \"imap.mail.me.com\", \"secret\": \"imap:owner@icloud.example\"}'),"
                 " ('club', 'imap', 'admin@club.example', 'Club', true,"
                 "  '{\"host\": \"mailcluster.loopia.se\", \"secret\": \"imap:admin@club.example\"}')")
    conn.execute("insert into my_address values ('owner@icloud.example', 'icloud')")
    conn.commit()
    return conn


def new_draft(c, probe, account="gmail", **fields):
    d = compose.start(c, probe, "new")
    fields = {"account_id": account, "to_addrs": ["oskar@nordvik.se"], "subject": "Hej", "body": "Hej Oskar", **fields}
    return compose.save_draft(c, d["id"], fields)


# ---------------------------------------------------------------- the confirmation

def test_a_send_without_a_confirmation_is_refused(cconn):
    s = send.Sender(FakeProbe(), transport_for_account=lambda a: Recorder())
    d = new_draft(cconn, s.probe)
    for token in (None, "", "made-up"):
        with pytest.raises(send.SendRefused) as e:
            s.send(cconn, d["id"], token)
        assert e.value.code in ("no_confirmation", "unknown_confirmation")
    assert [r["result"] for r in cconn.execute("select result from send_log")] == ["refused"] * 3


def test_a_confirmation_expires_after_five_minutes(cconn):
    clock = Clock()
    rec = Recorder()
    s = send.Sender(FakeProbe(), transport_for_account=lambda a: rec, book=send.Confirmations(clock=clock))
    d = new_draft(cconn, s.probe)
    token = s.confirm(cconn, d["id"])["token"]
    clock.t += send.TOKEN_TTL + 1
    with pytest.raises(send.SendRefused) as e:
        s.send(cconn, d["id"], token)
    assert e.value.code == "expired" and rec.sent == []


def test_a_change_after_confirming_needs_a_new_confirmation(cconn):
    clock = Clock()
    rec = Recorder()
    probe = FakeProbe(secrets={"gmail:owner@gmail.com", "imap:admin@club.example"})
    s = send.Sender(probe, transport_for_account=lambda a: rec, book=send.Confirmations(clock=clock))
    for change in ({"subject": "Hej!"}, {"body": "Hej Oskar."}, {"to_addrs": ["oskar@nordvik.se", "x@nordvik.se"]},
                   {"bcc_addrs": ["spy@example.com"]}, {"account_id": "club"}):
        d = new_draft(cconn, s.probe)
        token = s.confirm(cconn, d["id"])["token"]
        clock.t += 2
        compose.save_draft(cconn, d["id"], change)
        with pytest.raises(send.SendRefused) as e:
            s.send(cconn, d["id"], token)
        assert e.value.code in ("changed", "unknown_confirmation"), change
    assert rec.sent == []


def test_a_confirmation_is_used_once_and_only_after_a_moment(cconn):
    clock = Clock()
    rec = Recorder()
    s = send.Sender(FakeProbe(), transport_for_account=lambda a: rec, book=send.Confirmations(clock=clock))
    d = new_draft(cconn, s.probe)
    token = s.confirm(cconn, d["id"])["token"]
    with pytest.raises(send.SendRefused) as e:  # a script confirming and sending at once
        s.send(cconn, d["id"], token)
    assert e.value.code == "too_fast"
    token = s.confirm(cconn, d["id"])["token"]
    clock.t += 2
    assert s.send(cconn, d["id"], token)["sent"] is True
    assert len(rec.sent) == 1
    d2 = new_draft(cconn, s.probe)
    with pytest.raises(send.SendRefused):  # the same token again, for another draft
        s.send(cconn, d2["id"], token)


def test_a_token_from_another_confirmation_book_is_unknown(cconn):
    clock = Clock()
    web = send.Sender(FakeProbe(), transport_for_account=lambda a: Recorder(), book=send.Confirmations(clock=clock))
    rec = Recorder()
    job = send.Sender(FakeProbe(), transport_for_account=lambda a: rec, book=send.Confirmations(clock=clock))
    d = new_draft(cconn, web.probe)
    token = web.confirm(cconn, d["id"])["token"]
    clock.t += 2
    with pytest.raises(send.SendRefused) as e:
        job.send(cconn, d["id"], token)
    assert e.value.code == "unknown_confirmation" and rec.sent == []


def test_the_send_module_keeps_no_book_of_its_own():
    assert not [k for k, v in vars(send).items() if isinstance(v, (send.Confirmations, send.Sender))]


# ---------------------------------------------------------------- the web routes

def client(database, vault, **kw):
    return TestClient(app.create(Settings(home=vault.root.parent, dsn=database), allowed_hosts=["testserver"],
                                 argus_timers=False, **kw))


def test_confirm_and_send_come_only_from_the_page(cconn, database, vault):
    rec = Recorder()
    c = client(database, vault, send_probe=FakeProbe(), send_transport=lambda a: rec)
    d = c.post("/api/drafts", json={"mode": "new"}, headers={"X-Talos": "1"}).json()["draft"]
    fields = {"account_id": "gmail", "to_addrs": ["oskar@nordvik.se"], "subject": "Hej", "body": "Hej"}
    assert c.post(f"/api/drafts/{d['id']}/confirm", json=fields).status_code == 403
    assert c.post(f"/api/drafts/{d['id']}/confirm", json=fields, headers={"X-Talos": "1"}).status_code == 403
    assert c.post(f"/api/drafts/{d['id']}/confirm", json=fields,
                  headers={"X-Talos": "1", "Sec-Fetch-Site": "cross-site"}).status_code == 403
    r = c.post(f"/api/drafts/{d['id']}/send", json=fields, headers=SAME)
    assert r.status_code == 403 and r.json()["code"] == "no_confirmation"
    assert rec.sent == []


def test_the_web_send_refuses_a_mail_changed_after_the_confirmation(cconn, database, vault):
    clock = Clock()
    rec = Recorder()
    c = client(database, vault, send_probe=FakeProbe(), send_transport=lambda a: rec,
               send_book=send.Confirmations(clock=clock))
    d = c.post("/api/drafts", json={"mode": "new"}, headers={"X-Talos": "1"}).json()["draft"]
    fields = {"account_id": "gmail", "to_addrs": ["oskar@nordvik.se"], "subject": "Hej", "body": "Hej"}
    conf = c.post(f"/api/drafts/{d['id']}/confirm", json=fields, headers=SAME).json()
    assert conf["summary"]["from_addr"] == "owner@gmail.com" and conf["summary"]["to"] == ["oskar@nordvik.se"]
    clock.t += 2
    r = c.post(f"/api/drafts/{d['id']}/send", json={**fields, "to_addrs": ["other@nordvik.se"], "token": conf["token"]},
               headers=SAME)
    assert r.status_code == 403 and r.json()["code"] == "changed" and rec.sent == []
    conf = c.post(f"/api/drafts/{d['id']}/confirm", json=fields, headers=SAME).json()
    clock.t += 2
    r = c.post(f"/api/drafts/{d['id']}/send", json={**fields, "token": conf["token"]}, headers=SAME)
    assert r.status_code == 200 and len(rec.sent) == 1
    assert c.get(f"/api/drafts/{d['id']}").status_code == 404  # the draft goes once it is sent


# ---------------------------------------------------------------- the sending accounts

def test_the_work_account_cannot_be_picked_until_the_sign_in_carries_mail_send(cconn):
    rows = {r["id"]: r for r in compose.senders(cconn, FakeProbe(graph=False))}
    assert rows["work"]["pickable"] is False and rows["work"]["status"] == "needs permission"
    assert "API permissions" in rows["work"]["how"] and "talos auth graph work" in rows["work"]["how"]
    with pytest.raises(compose.ComposeError):
        compose.pickable(cconn, FakeProbe(graph=False), "work")
    with pytest.raises(compose.ComposeError):
        compose.set_default_account(cconn, FakeProbe(graph=False), "work")
    assert compose.pickable(cconn, FakeProbe(graph=True), "work")["address"] == "owner@company.example"


def test_imap_accounts_send_only_when_enabled_and_their_password_is_there(cconn):
    rows = {r["id"]: r for r in compose.senders(cconn, FakeProbe())}
    assert rows["gmail"]["pickable"] and "local" not in rows and "teams" not in rows
    assert rows["icloud"]["status"] == "off" and not rows["icloud"]["pickable"]
    assert rows["club"]["status"] == "no password"
    assert "security add-generic-password -s talos -a imap:admin@club.example -w" in rows["club"]["how"]
    rows = {r["id"]: r for r in compose.senders(cconn, FakeProbe(secrets={"imap:admin@club.example"}))}
    assert rows["club"]["pickable"] and not rows["gmail"]["pickable"]
    assert compose.smtp_server(cconn.execute("select * from account where id = 'club'").fetchone()) == \
        ("mailcluster.loopia.se", 587, "starttls")
    assert compose.smtp_server(cconn.execute("select * from account where id = 'icloud'").fetchone()) == \
        ("smtp.mail.me.com", 587, "starttls")


def test_a_draft_that_is_not_ready_cannot_be_confirmed(cconn):
    s = send.Sender(FakeProbe(), transport_for_account=lambda a: Recorder())
    d = compose.start(cconn, s.probe, "new")
    assert d["account_id"] is None  # no default: the choice is the owner's
    with pytest.raises(compose.ComposeError, match="choose the account"):
        s.confirm(cconn, d["id"])
    compose.save_draft(cconn, d["id"], {"account_id": "work", "to_addrs": ["a@nordvik.se"]})
    with pytest.raises(compose.ComposeError, match="needs permission"):
        s.confirm(cconn, d["id"])
    compose.save_draft(cconn, d["id"], {"account_id": "gmail", "to_addrs": ["not an address"]})
    with pytest.raises(compose.ComposeError, match="not an e-mail address"):
        s.confirm(cconn, d["id"])
    compose.save_draft(cconn, d["id"], {"to_addrs": []})
    with pytest.raises(compose.ComposeError, match="at least one recipient"):
        s.confirm(cconn, d["id"])


def test_a_new_mail_starts_from_the_default_account(cconn):
    probe = FakeProbe()
    compose.set_default_account(cconn, probe, "gmail")
    assert compose.start(cconn, probe, "new")["account_id"] == "gmail"
    assert [r["id"] for r in compose.senders(cconn, probe) if r["default"]] == ["gmail"]


# ---------------------------------------------------------------- replies and MIME

def _original(cconn, ingestor, account="work", **kw):
    raw = mf.make(frm="Oskar Nyström <oskar@nordvik.se>", to="owner@company.example",
                  cc="Helena Roos <helena.roos@nordvik.se>, o@company.example", subject="SV: Brandväggsfönster",
                  body="Kan du bekräfta fredag?\n\nMvh Oskar", msgid="<orig-1@nordvik.se>",
                  references="<first@nordvik.se>", **kw)
    mid = ingestor.ingest(account, raw, Location("Inkorgen", "x")).message_id
    cconn.commit()
    return mid


def test_a_reply_starts_from_the_account_the_original_came_to(cconn, ingestor):
    mid = _original(cconn, ingestor)
    probe = FakeProbe(graph=True)
    compose.set_default_account(cconn, probe, "gmail")
    d = compose.start(cconn, probe, "reply", mid)
    assert d["account_id"] == "work" and d["to_addrs"] == ["Oskar Nyström <oskar@nordvik.se>"]
    assert d["subject"] == "Re: Brandväggsfönster" and d["in_reply_to"] == "<orig-1@nordvik.se>"
    assert d["references_ids"] == ["<first@nordvik.se>", "<orig-1@nordvik.se>"]
    assert d["quoted"].splitlines()[1] == "> Kan du bekräfta fredag?"
    all_ = compose.start(cconn, probe, "reply_all", mid)
    assert all_["to_addrs"] == ["Oskar Nyström <oskar@nordvik.se>"]
    assert all_["cc_addrs"] == ["Helena Roos <helena.roos@nordvik.se>"]  # never the owner
    fwd = compose.start(cconn, probe, "forward", mid)
    assert fwd["to_addrs"] == [] and fwd["subject"] == "Fwd: Brandväggsfönster" and fwd["in_reply_to"] is None
    assert "---------- Forwarded message ----------" in fwd["quoted"]
    assert not [w for w in compose.warnings(cconn, d) if w["code"] == "reply_account"]
    moved = compose.save_draft(cconn, d["id"], {"account_id": "gmail"})
    assert [w["code"] for w in compose.warnings(cconn, moved) if w["level"] == "warn"][0] == "reply_account"


def test_a_reply_to_work_mail_without_mail_send_starts_with_no_account(cconn, ingestor):
    mid = _original(cconn, ingestor)
    probe = FakeProbe(graph=False)
    compose.set_default_account(cconn, probe, "gmail")
    assert compose.start(cconn, probe, "reply", mid)["account_id"] is None  # never quietly another account


def test_the_mime_of_a_new_mail_has_the_headers_and_never_a_bcc(cconn):
    probe = FakeProbe()
    compose.set_setting(cconn, "from_name", "Alex Lind")
    d = new_draft(cconn, probe, to_addrs=["Oskar <oskar@nordvik.se>"], cc_addrs=["helena.roos@nordvik.se"],
                  bcc_addrs=["hidden@nordvik.se"], subject="Fönster på fredag", body="Hej Oskar,\n\nFredag går bra.")
    out = compose.outgoing(cconn, probe, d["id"])
    assert out.recipients == ["oskar@nordvik.se", "helena.roos@nordvik.se", "hidden@nordvik.se"]
    msg = email.message_from_bytes(compose.build_mime(out).as_bytes(), policy=policy.default)
    assert msg["From"] == "Alex Lind <owner@gmail.com>"
    assert msg["To"] == "Oskar <oskar@nordvik.se>" and msg["Cc"] == "helena.roos@nordvik.se"
    assert msg["Bcc"] is None and b"hidden@" not in compose.build_mime(out).as_bytes()
    assert msg["Subject"] == "Fönster på fredag" and msg["Date"] and msg["Message-ID"].endswith("@gmail.com>")
    assert msg["In-Reply-To"] is None and msg.get_content_type() == "text/plain"
    assert msg.get_content().startswith("Hej Oskar,\n\nFredag går bra.")


def test_the_mime_of_a_reply_threads_and_quotes(cconn, ingestor):
    mid = _original(cconn, ingestor)
    probe = FakeProbe(graph=True)
    d = compose.start(cconn, probe, "reply", mid)
    compose.save_draft(cconn, d["id"], {"body": "Fredag passar."})
    msg = compose.build_mime(compose.outgoing(cconn, probe, d["id"]))
    assert msg["In-Reply-To"] == "<orig-1@nordvik.se>"
    assert msg["References"] == "<first@nordvik.se> <orig-1@nordvik.se>"
    text = msg.get_content()
    assert text.startswith("Fredag passar.\n\nOn ") and "> Kan du bekräfta fredag?" in text
    compose.save_draft(cconn, d["id"], {"include_quote": False})
    assert "Kan du" not in compose.build_mime(compose.outgoing(cconn, probe, d["id"])).get_content()


def test_a_formatted_signature_adds_a_cleaned_html_part(cconn):
    probe = FakeProbe()
    sig = compose.save_signature(cconn, {"name": "Work", "body_text": "Alex\nIT", "accounts": ["gmail"],
                                         "body_html": '<p><b>Alex</b></p><script>alert(1)</script>'
                                                      '<a href="https://x.se" onclick="evil()">x</a>'})
    assert "script" not in sig["body_html"] and "onclick" not in sig["body_html"] and "<b>Alex</b>" in sig["body_html"]
    d = new_draft(cconn, probe, body="Hej <du>", signature_id=sig["id"])
    msg = compose.build_mime(compose.outgoing(cconn, probe, d["id"]))
    assert msg.get_content_type() == "multipart/alternative"
    plain, html = [p.get_content() for p in msg.iter_parts()]
    assert plain.startswith("Hej <du>\n\nAlex\nIT")
    assert "Hej &lt;du&gt;" in html and "<b>Alex</b>" in html


# ---------------------------------------------------------------- signatures

def test_the_signature_follows_the_account_and_the_kind_of_mail(cconn):
    work = compose.save_signature(cconn, {"name": "Work", "body_text": "Company", "accounts": ["work"],
                                          "default_for": ["work"]})
    short = compose.save_signature(cconn, {"name": "Short", "body_text": "/S", "accounts": ["work", "gmail"],
                                           "for_new": False, "for_reply": True})
    home = compose.save_signature(cconn, {"name": "Home", "body_text": "Alex", "accounts": ["gmail"],
                                          "for_new": True, "for_reply": False})
    assert compose.signature_for(cconn, "work", "new")["id"] == work["id"]
    assert compose.signature_for(cconn, "work", "reply")["id"] == work["id"]      # the default wins
    assert compose.signature_for(cconn, "gmail", "new")["id"] == home["id"]      # the only one for new mail
    assert compose.signature_for(cconn, "gmail", "forward")["id"] == short["id"]  # forwards are replies
    assert compose.signature_for(cconn, "club", "new") is None
    compose.save_signature(cconn, {"name": "Short", "body_text": "/S", "accounts": ["work", "gmail"],
                                   "for_new": False, "for_reply": True, "default_for": ["work"]}, short["id"])
    assert compose.signature_for(cconn, "work", "reply")["id"] == short["id"]     # one default per account and kind
    assert compose.signature_for(cconn, "work", "new")["id"] == work["id"]
    assert cconn.execute("select count(*) n from signature_default where account_id = 'work'").fetchone()["n"] == 2
    probe = FakeProbe(graph=True)
    assert compose.start(cconn, probe, "new")["signature_id"] is None  # no account chosen yet
    compose.set_default_account(cconn, probe, "work")
    assert compose.start(cconn, probe, "new")["signature_id"] == work["id"]


def test_a_signature_needs_a_name_text_and_known_accounts(cconn):
    for bad in ({"body_text": "x", "accounts": ["gmail"]}, {"name": "x", "accounts": ["gmail"]},
                {"name": "x", "body_text": "x", "accounts": ["teams"]}, {"name": "x", "body_text": "x", "accounts": []},
                {"name": "x", "body_text": "x", "accounts": ["gmail"], "for_new": False, "for_reply": False},
                {"name": "x", "body_text": "x", "accounts": ["gmail"], "default_for": ["work"]}):
        with pytest.raises(compose.ComposeError):
            compose.save_signature(cconn, bad)


# ---------------------------------------------------------------- the rate limit and the log

def test_at_most_twenty_mails_an_hour(cconn):
    clock = Clock()
    s = send.Sender(FakeProbe(), transport_for_account=lambda a: Recorder(), book=send.Confirmations(clock=clock))
    for _ in range(send.RATE_LIMIT - 1):
        cconn.execute("insert into send_log (result, subject, by_whom) values ('sent', 'x', %s)", (personal.OWNER_ID,))
    cconn.execute("insert into send_log (result, subject, at, by_whom) values ('sent', 'old', now() - interval '2 hours', %s)",
                  (personal.OWNER_ID,))
    cconn.execute("insert into send_log (result, subject, by_whom) values ('refused', 'x', %s)", (personal.OWNER_ID,))
    d = new_draft(cconn, s.probe)
    token = s.confirm(cconn, d["id"])["token"]  # 19 in the past hour: one more is allowed
    clock.t += 2
    s.send(cconn, d["id"], token)
    d = new_draft(cconn, s.probe)
    with pytest.raises(send.SendRefused) as e:
        s.confirm(cconn, d["id"])
    assert e.value.code == "rate_limit"


def test_the_send_log_never_holds_the_body(cconn):
    clock = Clock()
    s = send.Sender(FakeProbe(), transport_for_account=lambda a: Recorder(), book=send.Confirmations(clock=clock))
    secret_text = "Det hemliga innehållet 4711"
    d = new_draft(cconn, s.probe, body=secret_text, bcc_addrs=["Dold <dold@nordvik.se>"])
    token = s.confirm(cconn, d["id"])["token"]
    clock.t += 2
    res = s.send(cconn, d["id"], token)
    row = cconn.execute("select * from send_log where id = %s", (res["log_id"],)).fetchone()
    assert row["result"] == "sent" and row["from_addr"] == "owner@gmail.com"
    assert row["to_addrs"] == ["oskar@nordvik.se"] and row["bcc_addrs"] == ["dold@nordvik.se"]
    assert row["subject"] == "Hej" and row["message_id"] == res["message_id"]
    assert secret_text not in repr(row)
    cols = {r["column_name"] for r in cconn.execute(
        "select column_name from information_schema.columns where table_name = 'send_log'")}
    assert not {c for c in cols if "body" in c or "text" in c or "content" in c}


# ---------------------------------------------------------------- warnings

def test_work_mail_from_gmail_and_unknown_domains_are_pointed_out(cconn, ingestor):
    ingestor.ingest("gmail", mf.make(frm="Anna <anna@nordvik.se>"), Location("[all]", "a"))
    cconn.commit()
    probe = FakeProbe()
    # a colleague on the owner's work domain, a known domain, and a new one
    d = new_draft(cconn, probe, to_addrs=["kollega@company.example", "anna@nordvik.se", "ny@okand-firma.se"])
    codes = {w["code"]: w for w in compose.warnings(cconn, d)}
    assert codes["work_from_personal"]["level"] == "warn"
    assert codes["new_domain"]["level"] == "note" and "okand-firma.se" in codes["new_domain"]["text"]
    assert "nordvik.se" not in codes["new_domain"]["text"]
    d = compose.save_draft(cconn, d["id"], {"account_id": "work", "to_addrs": ["kompis@gmail.com"]})
    assert "personal_from_work" in {w["code"] for w in compose.warnings(cconn, d)}


def test_addresses_are_suggested_from_the_people_the_owner_has_mailed(cconn, ingestor):
    ingestor.ingest("gmail", mf.make(frm="Alex <owner@gmail.com>", to="Oskar Nyström <oskar@nordvik.se>"),
                    Location("[all]", "s"))
    ingestor.ingest("gmail", mf.make(frm="Oskarsson <oskarsson@x.se>", subject="nyhetsbrev"), Location("[all]", "n"))
    cconn.commit()
    rows = compose.suggest(cconn, "oska")
    assert [r["address"] for r in rows][:2] == ["oskar@nordvik.se", "oskarsson@x.se"]
    assert rows[0]["sent"] == 1


# ---------------------------------------------------------------- end to end, through a fake SMTP server

def test_a_confirmed_mail_goes_through_smtp_to_the_fake_server(cconn, database, vault):
    """The whole path: the page confirms, waits, sends; SmtpTransport logs in with the app password
    and hands the message to the loopback fake. The envelope carries the Bcc; the message does not."""
    clock = Clock()
    with FakeSmtp() as smtp:
        def transport(account):
            assert account["id"] == "gmail"
            return send.SmtpTransport("127.0.0.1", smtp.port, "none", account["address"], lambda: "app-password")
        c = client(database, vault, send_probe=FakeProbe(), send_transport=transport,
                   send_book=send.Confirmations(clock=clock))
        d = c.post("/api/drafts", json={"mode": "new"}, headers={"X-Talos": "1"}).json()["draft"]
        fields = {"account_id": "gmail", "to_addrs": ["Oskar <oskar@nordvik.se>"], "bcc_addrs": ["dold@nordvik.se"],
                  "subject": "Test från Talos", "body": "Hej!\n.\nEn rad med bara en punkt."}
        conf = c.post(f"/api/drafts/{d['id']}/confirm", json=fields, headers=SAME).json()
        clock.t += 2
        r = c.post(f"/api/drafts/{d['id']}/send", json={**fields, "token": conf["token"]}, headers=SAME)
        assert r.status_code == 200, r.text
    got = smtp.received[0]
    assert got.user == "owner@gmail.com" and got.password == "app-password"
    assert got.mail_from == "owner@gmail.com"
    assert got.rcpt_to == ["oskar@nordvik.se", "dold@nordvik.se"]
    msg = email.message_from_bytes(got.data, policy=policy.default)
    assert msg["Subject"] == "Test från Talos" and msg["Bcc"] is None and b"dold@" not in got.data
    assert msg.get_content().replace("\r\n", "\n") == "Hej!\n.\nEn rad med bara en punkt.\n"
    assert msg["Message-ID"] == r.json()["message_id"]
    assert cconn.execute("select result from send_log").fetchone()["result"] == "sent"


def test_a_refused_recipient_or_login_is_a_failed_send_that_is_logged(cconn):
    clock = Clock()
    with FakeSmtp(fail_login=True) as smtp:
        s = send.Sender(FakeProbe(), book=send.Confirmations(clock=clock), transport_for_account=lambda a:
                        send.SmtpTransport("127.0.0.1", smtp.port, "none", "u", lambda: "wrong"))
        d = new_draft(cconn, s.probe)
        token = s.confirm(cconn, d["id"])["token"]
        clock.t += 2
        with pytest.raises(send.SendFailed):
            s.send(cconn, d["id"], token)
    row = cconn.execute("select result, detail from send_log").fetchone()
    assert row["result"] == "failed" and "SMTPAuthenticationError" in row["detail"]
    assert compose.get_draft(cconn, d["id"]) is not None  # the draft stays, to try again


def test_plain_smtp_is_refused_off_the_loopback():
    with pytest.raises(ValueError):
        send.SmtpTransport("smtp.gmail.com", 25, "none", "u", lambda: "p")


def test_graph_sends_the_built_mime_to_send_mail():
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), auth=request.headers["authorization"], body=request.content,
                    ctype=request.headers["content-type"])
        return httpx.Response(202)

    out = compose.Outgoing(draft_id=1, account_id="work", from_addr="owner@company.example", from_name=None,
                           to=["oskar@nordvik.se"], cc=[], bcc=[], subject="Hej", text="Hej\n")
    t = send.GraphTransport(lambda: "tok", http=httpx.Client(transport=httpx.MockTransport(handler)))
    t.deliver(compose.build_mime(out), out.from_addr, out.recipients)
    assert seen["url"] == send.GRAPH_SEND_URL and seen["auth"] == "Bearer tok" and seen["ctype"] == "text/plain"
    assert b"Subject: Hej" in base64.b64decode(seen["body"])
