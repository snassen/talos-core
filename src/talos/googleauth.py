"""Signing in to Google (the owner's Gmail account) for the calendar, with OAuth, as an installed app.

Google's calendar takes no app password, so Talos signs in like a desktop app: the owner creates an
OAuth client of type "Desktop app" in their own Google Cloud project (docs/calendar.md says how) and
stores its JSON in the Keychain themselves:

    security add-generic-password -s talos -a google-oauth-client -w

Then `talos auth google` opens Google's consent page in the owner's browser; Google sends the answer back to a
one-time listener on 127.0.0.1, and the refresh token is kept in the Keychain (google-token:<account>).
The password never passes through Talos, and PKCE ties the answer to this sign-in.

SCOPES: the calendar list (read) and events (read and write). Nothing touches the mail.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets as pysecrets
import threading
import time
import urllib.parse
from typing import Callable

import httpx

from talos import secrets

SCOPES = ["https://www.googleapis.com/auth/calendar.calendarlist.readonly",
          "https://www.googleapis.com/auth/calendar.events"]
CLIENT_KEY = "google-oauth-client"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def token_key(account_id: str) -> str:
    return f"google-token:{account_id}"


def _client() -> dict:
    raw = json.loads(secrets.get(CLIENT_KEY))
    c = raw.get("installed") or raw.get("web") or raw
    if not c.get("client_id"):
        raise secrets.MissingSecret(f"The Keychain item {CLIENT_KEY!r} holds no client_id; store the client's JSON there")
    return c


def signed_in(account_id: str) -> bool:
    return secrets.exists(token_key(account_id))


def sign_in(account_id: str, *, open_browser: Callable[[str], object] | None = None, show=print,
            client: httpx.Client | None = None, timeout: float = 300) -> str:
    """The consent in the owner's browser; returns the scopes granted. The refresh token goes to the Keychain.
    (client is the HTTP client for the token exchange; it was once named http, which hid the http
    module this function needs for its one-shot local server.)"""
    import webbrowser
    c = _client()
    verifier = base64.urlsafe_b64encode(pysecrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = pysecrets.token_urlsafe(16)
    got: dict = {}

    class Answer(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — the stdlib's name
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if q.get("state") == state:
                got.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Talos: you can close this tab.".encode())

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Answer)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": " ".join(SCOPES),
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    worker = threading.Thread(target=lambda: [server.handle_request() for _ in range(5) if not got], daemon=True)
    worker.start()
    show(f"Opening Google's consent page. If it does not open, visit:\n{url}")
    (open_browser or webbrowser.open)(url)
    worker.join(timeout)
    server.server_close()
    if "code" not in got:
        raise RuntimeError(f"Google gave no consent ({got.get('error', 'no answer in time')})")
    r = (client or httpx.Client(timeout=30)).post(TOKEN_URL, data={
        "client_id": c["client_id"], "client_secret": c.get("client_secret", ""), "code": got["code"],
        "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": redirect})
    if r.status_code != 200:
        raise RuntimeError(f"Google refused the sign-in: {r.status_code} {r.text[:200]}")
    tok = r.json()
    if not tok.get("refresh_token"):
        raise RuntimeError("Google gave no refresh token; remove Talos's access at myaccount.google.com and sign in again")
    secrets.put(token_key(account_id), json.dumps({"refresh_token": tok["refresh_token"], "scope": tok.get("scope", "")}))
    return tok.get("scope", "")


class GoogleAuth:
    """Access tokens from the stored refresh token, refreshed a minute before they run out."""

    def __init__(self, account_id: str, http: httpx.Client | None = None):
        self.account_id = account_id
        self.http = http
        self._token: str | None = None
        self._until = 0.0

    def token(self, *, force_refresh: bool = False) -> str:
        if self._token and not force_refresh and time.time() < self._until:
            return self._token
        stored = json.loads(secrets.get(token_key(self.account_id)))
        c = _client()
        r = (self.http or httpx.Client(timeout=30)).post(TOKEN_URL, data={
            "client_id": c["client_id"], "client_secret": c.get("client_secret", ""),
            "refresh_token": stored["refresh_token"], "grant_type": "refresh_token"})
        if r.status_code != 200:
            raise secrets.MissingSecret(f"The Google sign-in has expired or was withdrawn ({r.status_code}). Run: talos auth google")
        body = r.json()
        self._token, self._until = body["access_token"], time.time() + int(body.get("expires_in", 3600)) - 60
        return self._token
