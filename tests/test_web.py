import mailfactory as mf
from starlette.testclient import TestClient

from talos import db, webauth
from talos.config import Settings
from talos.ingest import Location
from talos.web import app


def client(database, vault):
    return TestClient(app.create(Settings(home=vault.root.parent, dsn=database),
                                  allowed_hosts=["testserver"]))


def test_the_owner_endpoint_gives_the_owners_id_and_each_account_as_the_page_shows_it(conn, vault, database):
    r = client(database, vault).get("/api/owner")
    assert r.status_code == 200
    o = r.json()
    assert o["id"] == "owner"   # owner.json is absent in the tests: the default
    assert [a["id"] for a in o["accounts"]] == ["gmail", "work", "icloud", "club", "teams", "local"]  # the example's order
    assert o["accounts"][1] == {"id": "work", "name": "Work", "letter": "W", "color": 2, "ink": None}
    assert o["accounts"][-1] == {"id": "local", "name": "Imported", "letter": "I", "color": "other", "ink": None}


def test_the_api_lists_searches_and_opens_messages(conn, ingestor, vault, database):
    mid = ingestor.ingest("gmail", mf.make(subject="Din faktura från Telia", attachments=[
        ("faktura.pdf", "application/pdf", mf.pdf("Faktura 1")),
        ("evil.html", "text/html", b"<script>alert(1)</script>")]),
        Location("[all]", "a", labels=["Receipts"], flags=["seen"])).message_id
    conn.commit()
    c = client(database, vault)
    assert c.get("/api/overview").json()["messages"] == 1
    res = c.get("/api/messages", params={"q": "fakturor"}).json()
    assert res["total"] == 1 and res["rows"][0]["labels"] == ["Receipts"]
    assert c.get("/api/messages", params={"label": "Nope"}).json()["total"] == 0
    m = c.get(f"/api/messages/{mid}").json()
    assert {a["filename"] for a in m["attachments"]} == {"faktura.pdf", "evil.html"}
    assert c.get(f"/api/messages/{mid}/raw").content.startswith(b"From:") or b"Subject:" in c.get(f"/api/messages/{mid}/raw").content


def test_attachments_cannot_run_script_in_the_app(conn, ingestor, vault, database):
    ingestor.ingest("gmail", mf.make(attachments=[("evil.html", "text/html", b"<script>alert(1)</script>"),
                                                  ("ok.pdf", "application/pdf", mf.pdf("x"))]), Location("[all]", "a"))
    conn.commit()
    ids = {r["filename"]: r["id"] for r in conn.execute("select id, filename from attachment")}
    c = client(database, vault)
    evil = c.get(f"/api/attachments/{ids['evil.html']}")
    assert evil.headers["content-type"] == "application/octet-stream"
    assert evil.headers["content-disposition"].startswith("attachment")
    assert evil.headers["content-security-policy"] == "sandbox"
    pdf = c.get(f"/api/attachments/{ids['ok.pdf']}")
    assert pdf.headers["content-type"] == "application/pdf" and pdf.headers["content-disposition"].startswith("inline")


def test_other_hosts_and_headerless_posts_are_refused(conn, vault, database):
    c = client(database, vault)
    assert c.get("/api/overview", headers={"host": "evil.example"}).status_code == 400
    assert c.post("/api/rules/preview", json={"conditions": []}).status_code == 403
    ok = c.post("/api/rules/preview", json={"conditions": [{"field": "subject", "op": "contains", "value": "x"}]},
                headers={"X-Talos": "1"})
    assert ok.status_code == 200 and ok.json()["count"] == 0


def test_the_ui_shell_is_served(database, vault):
    c = client(database, vault)
    r = c.get("/")
    assert r.status_code == 200 and "/static/app.js" in r.text
    assert c.get("/static/app.js").status_code == 200


def test_the_ui_script_parses():
    import shutil
    import subprocess
    from pathlib import Path

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    js = Path(__file__).parent.parent / "src" / "talos" / "web" / "static" / "app.js"
    r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_attachment_names_outside_latin_1_download_with_an_exact_utf_8_name(conn, ingestor, vault, database):
    ingestor.ingest("gmail", mf.make(attachments=[("Faktura – september €.pdf", "application/pdf", mf.pdf("x")),
                                                  ("Lön.txt", "text/plain", b"hej")]), Location("[all]", "a"))
    conn.commit()
    ids = {r["filename"]: r["id"] for r in conn.execute("select id, filename from attachment")}
    c = client(database, vault)
    r = c.get(f"/api/attachments/{ids['Faktura – september €.pdf']}")
    assert r.status_code == 200
    assert r.headers["content-disposition"] == ("inline; filename=\"Faktura september .pdf\";"
                                                " filename*=UTF-8''Faktura%20%E2%80%93%20september%20%E2%82%AC.pdf")
    assert 'filename="Lon.txt"' in c.get(f"/api/attachments/{ids['Lön.txt']}").headers["content-disposition"]


def test_a_filename_cannot_start_a_new_header(conn, ingestor, vault, database):
    ingestor.ingest("gmail", mf.make(attachments=[("a.pdf", "application/pdf", mf.pdf("x"))]), Location("[all]", "a"))
    conn.execute("update attachment set filename = %s", ("a\r\nSet-Cookie: stolen=1\r\n.pdf",))
    conn.commit()
    aid = conn.execute("select id from attachment").fetchone()["id"]
    r = client(database, vault).get(f"/api/attachments/{aid}")
    assert r.status_code == 200 and "set-cookie" not in r.headers
    assert "\r" not in r.headers["content-disposition"] and "\n" not in r.headers["content-disposition"]


def test_the_app_answers_only_localhost_unless_told_otherwise(database, vault):
    c = TestClient(app.create(Settings(home=vault.root.parent, dsn=database), auth_store=webauth.MemoryStore(),
                              auth_notifier=lambda *a: None))
    assert c.get("/api/overview").status_code == 400  # the test client's Host is "testserver"
    local = {"host": "127.0.0.1:7420"}
    assert c.get("/api/overview", headers=local).status_code == 401  # past the host check, at the door
    with db.connect(database) as conn:
        c.cookies.set(webauth.COOKIE, webauth.mint(conn, minutes=5, origin="local:test"))
    assert c.get("/api/overview", headers=local).status_code == 200


def test_messages_can_be_narrowed_to_mail_or_to_teams(conn, ingestor, vault, database):
    ingestor.ingest("gmail", mf.make(subject="Ett mejl"), Location("[all]", "mail-1"))
    mid = ingestor.ingest("gmail", mf.make(subject="Ett chattmeddelande"), Location("[all]", "chat-1")).message_id
    conn.execute("update message set medium = 'teams_chat' where id = %s", (mid,))
    conn.commit()
    c = client(database, vault)
    subjects = lambda medium: {r["subject"] for r in c.get("/api/messages", params={"medium": medium}).json()["rows"]}
    assert subjects("email") == {"Ett mejl"}
    assert subjects("teams") == {"Ett chattmeddelande"}
    assert subjects("") == {"Ett mejl", "Ett chattmeddelande"}


def test_sync_now_runs_the_mail_accounts_only(conn, vault, database):
    conn.execute("insert into account (id, provider, address) values ('teams', 'teams', 'o@company.example')")
    conn.commit()
    started = []

    class Proc:
        returncode = 0

        def __init__(self, args, stdout, **kw):
            started.append(args)
            stdout.write("work: seen=3 added=1 updated=0 gone=0 failed=0\ngmail: another sync is running; skipped\n")
            self.done = False

        def poll(self):
            return 0 if self.done else None

    c = TestClient(app.create(Settings(home=vault.root.parent, dsn=database), allowed_hosts=["testserver"],
                              sync_runner=Proc))
    assert c.get("/api/sync").json()["available"] == ["gmail", "work"]  # not local, not Teams; by id
    assert c.post("/api/sync", json={}).status_code == 403  # no X-Talos header
    assert c.post("/api/sync", json={"accounts": ["teams"]}, headers={"X-Talos": "1"}).status_code == 400
    st = c.post("/api/sync", json={}, headers={"X-Talos": "1"}).json()
    assert st["running"] and started[0][-4:] == ["sync", "gmail", "work", "--then-rules"]
    c.post("/api/sync", json={}, headers={"X-Talos": "1"})
    assert len(started) == 1  # one at a time
    Proc.poll = lambda self: 0  # the run ends
    done = c.get("/api/sync").json()
    assert not done["running"] and done["exit"] == 0
    assert done["results"] == {"work": {"added": 1}, "gmail": {"note": "another sync is running; skipped"}}


def test_the_overview_is_kept_and_counted_again_when_mail_arrives(conn, ingestor, vault, database):
    import mailfactory as mf
    from talos.ingest import Location
    ingestor.ingest("gmail", mf.make(subject="first"), Location("INBOX", "1"))
    conn.commit()
    c = client(database, vault)
    first = c.get("/api/overview").json()
    assert first["messages"] == 1 and first["stale"] is False
    assert c.get("/api/overview").json()["computed_at"] == first["computed_at"]   # kept, not counted again
    ingestor.ingest("gmail", mf.make(subject="second"), Location("INBOX", "2"))
    conn.commit()
    assert c.get("/api/overview?refresh=1").json()["messages"] == 2               # counted again when asked


def test_a_saved_aggregation_shows_at_once_when_it_is_added_or_removed(conn, ingestor, vault, database):
    c = client(database, vault)
    H = {"X-Talos": "1"}
    assert c.get("/api/aggregations").json()["rows"] == []
    aid = c.post("/api/aggregations", json={"name": "By sender", "query": "direction=in", "group_by": "sender"}, headers=H).json()["id"]
    got = c.get("/api/aggregations").json()
    assert [r["id"] for r in got["rows"]] == [aid] and got["stale"] is False
    assert c.post(f"/api/aggregations/{aid}/remove", json={}, headers=H).status_code == 200
    assert c.get("/api/aggregations").json()["rows"] == []
