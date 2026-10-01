"""Reaching the web UI from other tailnet devices, through `tailscale serve`."""

import json

from starlette.testclient import TestClient

from talos import db, webauth
from talos.config import Settings
from talos.web import app

TAILNET = "studio.example.ts.net"


def client(database, vault, web=None):
    """The app as `talos serve` builds it (the door shut), with a signed-in session: these tests are
    about which hosts and tailnet users get through, which comes before the door."""
    home = vault.root.parent
    if web is not None:
        (home / "web.json").write_text(json.dumps(web))
    c = TestClient(app.create(Settings(home=home, dsn=database), auth_store=webauth.MemoryStore(),
                              auth_notifier=lambda *a: None))
    with db.connect(database) as conn:
        c.cookies.set(webauth.COOKIE, webauth.mint(conn, minutes=10, origin="local:test"))
    return c


def test_without_web_json_only_localhost_is_served(database, vault):
    c = client(database, vault)
    assert c.get("/api/changesets", headers={"host": "127.0.0.1:7420"}).status_code == 200
    assert c.get("/api/changesets", headers={"host": f"{TAILNET}:8443",
                                             "tailscale-user-login": "me@example.com"}).status_code == 400


def test_a_tailnet_host_needs_an_allowed_tailscale_user(database, vault):
    c = client(database, vault, {"tailnet_hosts": [TAILNET], "tailnet_users": ["me@example.com"]})
    ok = c.get("/api/changesets", headers={"host": f"{TAILNET}:8443", "tailscale-user-login": "Me@Example.com"})
    assert ok.status_code == 200
    assert c.get("/api/changesets", headers={"host": f"{TAILNET}:8443"}).status_code == 403
    assert c.get("/api/changesets", headers={"host": f"{TAILNET}:8443",
                                             "tailscale-user-login": "someone@else.com"}).status_code == 403
    # localhost is unchanged, and other names are still refused
    assert c.get("/api/changesets", headers={"host": "localhost:7420"}).status_code == 200
    assert c.get("/api/changesets", headers={"host": "evil.example.com"}).status_code == 400
