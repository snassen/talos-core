"""Signing in to Talos Web (talos.webauth, web.gate): no session, no data."""

import time

import mailfactory as mf
from starlette.testclient import TestClient

from talos import webauth
from talos.config import Settings
from talos.ingest import Location
from talos.web import app

PASSWORD = "correct horse battery staple"
SECRET = webauth.new_totp_secret()
RECOVERY = ["aaaa-bbbb-cccc", "dddd-eeee-ffff"]
X = {"X-Talos": "1"}


def store():
    s = webauth.MemoryStore()
    webauth.setup(s, PASSWORD, SECRET, RECOVERY)
    return s


def door(database, vault, *, st=None, reads=None, notes=None):
    return TestClient(app.create(Settings(home=vault.root.parent, dsn=database), allowed_hosts=["testserver"],
                                 auth_store=st if st is not None else store(), reads=reads,
                                 auth_notifier=(lambda t, x: notes.append(t)) if notes is not None else None))


def code(offset=0):
    return webauth.totp_at(SECRET, int(time.time() // webauth.TOTP_STEP) + offset)


def sign_in(c, the_code=None):
    return c.post("/auth/login", json={"password": PASSWORD, "code": the_code or code()}, headers=X)


def test_totp_codes_follow_rfc_6238():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # the RFC's key, "12345678901234567890"
    assert webauth.totp_at(secret, 59 // 30) == "287082"  # the RFC's 94287082, six digits
    assert webauth.totp_step("287 082", secret, now=59) == 1 and webauth.totp_step("287083", secret, now=59) is None


def test_passwords_are_kept_as_scrypt_hashes_never_as_text():
    s = store()
    assert PASSWORD not in s[webauth.PASSWORD_KEY] and s[webauth.PASSWORD_KEY].startswith("scrypt$")
    assert webauth.check_password(PASSWORD, s[webauth.PASSWORD_KEY])
    assert not webauth.check_password(PASSWORD + "!", s[webauth.PASSWORD_KEY])


def test_nothing_but_the_sign_in_page_answers_without_a_session(conn, vault, database):
    c = door(database, vault)
    assert c.get("/api/overview").status_code == 401
    assert c.get("/api/messages").json()["signin"] is True
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert c.get("/login").status_code == 200 and c.get("/static/login.js").status_code == 200
    assert c.get("/argus/status").status_code == 401


def test_the_right_password_and_code_open_a_session_and_the_cookie_is_locked_down(conn, vault, database):
    notes = []
    c = door(database, vault, notes=notes)
    r = sign_in(c)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert c.get("/api/overview").status_code == 200
    assert notes == ["Talos: signed in"]
    assert conn.execute("select count(*) as n from web_session").fetchone()["n"] == 1
    assert webauth.PASSWORD_KEY and PASSWORD not in str(conn.execute("select * from web_session").fetchall())


def test_a_wrong_password_or_code_is_refused_and_five_lock_the_door(conn, vault, database):
    notes = []
    c = door(database, vault, notes=notes)
    assert c.post("/auth/login", json={"password": "nope", "code": code()}, headers=X).status_code == 401
    assert c.post("/auth/login", json={"password": PASSWORD, "code": "000000"}, headers=X).status_code == 401
    for _ in range(3):
        r = c.post("/auth/login", json={"password": "nope", "code": "1"}, headers=X)
    assert r.status_code == 429 and r.json()["locked_until"]
    assert sign_in(c).status_code == 429  # even the right ones, until the window has passed
    assert "Talos: signing in locked" in notes
    assert c.post("/auth/login", json={"password": PASSWORD, "code": code()}).status_code == 403  # no X-Talos


def test_a_code_works_once_and_a_recovery_code_once(conn, vault, database):
    c = door(database, vault)
    the_code = code()
    assert sign_in(c, the_code).status_code == 200
    assert sign_in(TestClient(c.app), the_code).status_code == 401  # replayed
    other = TestClient(c.app)
    assert sign_in(other, "AAAA-BBBB-CCCC").status_code == 200
    assert sign_in(TestClient(c.app), "aaaa-bbbb-cccc").status_code == 401  # used up


def test_signing_out_ends_the_session_and_everywhere_ends_the_others(conn, vault, database):
    c = door(database, vault)
    sign_in(c, code(-1))
    other = TestClient(c.app)
    sign_in(other, code())
    assert c.post("/auth/signout-all", json={}, headers=X).json()["ended"] == 1
    assert other.get("/api/overview").status_code == 401 and c.get("/api/overview").status_code == 200
    c.post("/auth/logout", json={}, headers=X)
    assert c.get("/api/overview").status_code == 401


def test_reading_many_messages_fast_stops_until_a_code_is_entered(conn, ingestor, vault, database):
    ids = [ingestor.ingest("gmail", mf.make(subject=f"Hej {i}", msgid=f"<r{i}@test.invalid>"),
                           Location("[all]", f"r{i}")).message_id for i in range(4)]
    conn.commit()
    notes = []
    c = door(database, vault, reads=webauth.ReadBudget(budget=3, window=600), notes=notes)
    sign_in(c, code(-1))
    for i in ids[:3]:
        assert c.get(f"/api/messages/{i}").status_code == 200
    r = c.get(f"/api/messages/{ids[3]}")
    assert r.status_code == 429 and r.json()["step_up"] is True
    assert c.get("/api/overview").status_code == 200  # lists and pages still work
    assert "Talos: reading paused" in notes
    assert c.post("/auth/step-up", json={"code": "123"}, headers=X).status_code == 401
    assert c.post("/auth/step-up", json={"code": code()}, headers=X).status_code == 200
    assert c.get(f"/api/messages/{ids[3]}").status_code == 200


def test_every_response_carries_the_security_headers(conn, vault, database):
    c = door(database, vault)
    sign_in(c)
    page = c.get("/")
    csp = page.headers["content-security-policy"]
    assert "script-src 'self' 'sha256-" in csp and "object-src 'none'" in csp and "frame-ancestors 'self'" in csp
    api = c.get("/api/overview")
    assert api.headers["cache-control"] == "no-store" and api.headers["x-content-type-options"] == "nosniff"
    assert api.headers["referrer-policy"] == "no-referrer"


def test_the_door_cannot_be_left_open_outside_the_tests(conn, vault, database):
    # Built for the real host names and without a store: the Keychain store, and the door shut.
    c = TestClient(app.create(Settings(home=vault.root.parent, dsn=database), allowed_hosts=["127.0.0.1"]),
                   base_url="http://127.0.0.1")
    assert c.get("/api/overview").status_code == 401


def test_a_session_made_on_this_mac_is_logged_and_short(conn, vault, database):
    token = webauth.mint(conn, minutes=500, origin="local:cli")
    c = door(database, vault)
    c.cookies.set(webauth.COOKIE, token)
    assert c.get("/api/overview").status_code == 200
    row = conn.execute("select expires_at - created_at as life from web_session").fetchone()
    assert row["life"].total_seconds() <= 120 * 60
    assert conn.execute("select kind from web_event").fetchone()["kind"] == "session_minted"


def test_before_setup_nobody_can_sign_in(conn, vault, database):
    c = door(database, vault, st=webauth.MemoryStore())
    assert c.get("/auth/status").json()["set_up"] is False
    assert "talos web setup" in sign_in(c).json()["error"]


def test_nothing_that_returns_mail_is_on_the_public_list():
    from talos.web import gate
    for path in ["/", "/api/messages", "/api/messages/1", "/api/messages/1/raw", "/api/attachments/1", "/api/threads/1",
                 "/api/overview", "/api/space", "/auth/me", "/auth/step-up", "/argus/status", "/login/../api/messages"]:
        assert not gate.PUBLIC.match(path), path
    for path in ["/login", "/favicon.ico", "/auth/login", "/auth/status", "/static/app.js", "/argus/checkin/talos-sync"]:
        assert gate.PUBLIC.match(path), path


def test_the_tab_icon_is_served_without_a_session_and_the_sign_in_page_names_it_by_its_digest(conn, vault, database):
    c = door(database, vault)
    r = c.get("/favicon.ico", follow_redirects=False)
    assert r.status_code == 200 and r.headers["content-type"] == "image/x-icon"
    assert r.content == (app.STATIC / "icons" / "beacon-tab.ico").read_bytes()
    page = c.get("/login").text
    assert "beacon-tab.ico?v=" in page and "beacon-tab.svg?v=" in page


def test_talos_serve_builds_the_app_with_the_door_shut():
    import inspect
    from talos import cli
    src = inspect.getsource(cli.cmd_serve)
    assert "app.create(s)" in src and "testserver" not in src and "auth_store" not in src
