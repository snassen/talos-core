"""The door of Talos Web: no session, no data (talos.webauth, docs/security.md).

Gate is ASGI middleware in front of every route. What passes without a session is only what holds
no mail: the sign-in page and its API, the static files (code and icons), /favicon.ico, and the Argus check-ins,
which carry their own token. Everything else needs a live session cookie. A page request without
one is sent to /login; an API request gets 401.

It also counts reads (webauth.ReadBudget): whole messages, conversations and attachments, per
session. When a session has used its budget, the reads stop with 429 until an authenticator code
is entered again.

Security headers go on every response: a content security policy for the pages (scripts only from
/static, no framing by others), no sniffing, no referrer, and HSTS on the tailnet's https name.
"""

from __future__ import annotations

import hashlib
import base64
import re
from urllib.parse import quote

from starlette.responses import JSONResponse, RedirectResponse

from talos import webauth

PUBLIC = re.compile(r"^/(login|favicon\.ico|auth/(login|status)|static/.*|argus/(checkin|fail)/[\w.-]+)$")
CONTENT = re.compile(r"^/api/(messages/\d+(/raw|/html)?|threads/\d+|attachments/\d+)$")
ARGUS_STATUS = "/argus/status"


def inline_script_hashes(html: str) -> list[str]:
    """CSP hashes for the page's inline scripts (index.html sets the theme before the styles load)."""
    return ["'sha256-" + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() + "'"
            for s in re.findall(r"<script>(.*?)</script>", html, re.S)]


def page_csp(script_hashes: list[str]) -> str:
    return ("default-src 'self'; script-src 'self' " + " ".join(script_hashes) + "; style-src 'self' 'unsafe-inline';"
            " img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; frame-src 'self' blob:;"
            " object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'")


def origin_of(scope, headers: dict, tailnet_hosts: set[str]) -> str:
    host = headers.get("host", "").split(":")[0].lower()
    if host in tailnet_hosts:
        return "tailnet:" + headers.get("tailscale-user-login", "?")
    client = scope.get("client") or ("?", 0)
    return f"local:{client[0]}"


def cookie_header(token: str, *, secure: bool, max_age: int) -> str:
    return (f"{webauth.COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
            + ("; Secure" if secure else ""))


def token_from(headers: dict) -> str | None:
    for part in headers.get("cookie", "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == webauth.COOKIE and v:
            return v
    return None


class Gate:
    def __init__(self, app, *, conn, reads: webauth.ReadBudget, notify, tailnet_hosts: list[str], argus_token,
                 csp: str, enforce: bool = True):
        self.app, self.conn, self.reads, self.notify = app, conn, reads, notify
        self.tailnet_hosts = {h.lower() for h in tailnet_hosts}
        self.argus_token, self.csp, self.enforce = argus_token, csp, enforce
        self.stopped: set[str] = set()  # sessions whose bulk stop was announced

    def _headers_wrapper(self, send, host: str, path: str):
        async def wrapped(message):
            if message["type"] == "http.response.start":
                hs = list(message.get("headers", []))
                have = {k.lower() for k, _ in hs}
                add = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
                       (b"x-frame-options", b"SAMEORIGIN"),
                       (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()")]
                ctype = dict(hs).get(b"content-type", b"")
                if ctype.startswith(b"text/html") and b"content-security-policy" not in have:
                    add.append((b"content-security-policy", self.csp.encode()))
                if (path.startswith(("/api/", "/auth/"))) and b"cache-control" not in have:
                    add.append((b"cache-control", b"no-store"))
                if host in self.tailnet_hosts:
                    add.append((b"strict-transport-security", b"max-age=31536000"))
                message["headers"] = hs + [(k, v) for k, v in add if k not in have]
            await send(message)
        return wrapped

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        path = scope["path"]
        host = headers.get("host", "").split(":")[0].lower()
        send = self._headers_wrapper(send, host, path)
        scope.setdefault("state", {})
        scope["state"]["origin"] = origin_of(scope, headers, self.tailnet_hosts)
        scope["state"]["secure"] = host in self.tailnet_hosts or headers.get("x-forwarded-proto") == "https"
        if not self.enforce or PUBLIC.match(path):
            return await self.app(scope, receive, send)
        token = token_from(headers)
        from starlette.concurrency import run_in_threadpool
        session = await run_in_threadpool(self._session, token)
        if session is None and path == ARGUS_STATUS:
            auth = headers.get("authorization", "")
            if auth[:7].lower() == "bearer " and await run_in_threadpool(self.argus_token, auth[7:].strip()):
                return await self.app(scope, receive, send)
        if session is None:
            if path.startswith(("/api/", "/auth/", "/argus/")):
                return await JSONResponse({"error": "sign in first", "signin": True}, status_code=401)(scope, receive, send)
            nxt = quote(path + ("?" + scope["query_string"].decode() if scope.get("query_string") else ""))
            return await RedirectResponse("/login?next=" + nxt, status_code=303)(scope, receive, send)
        scope["state"]["session"] = session
        if scope["method"] == "GET" and CONTENT.match(path) and not self.reads.take(session.id):
            if session.id not in self.stopped:
                self.stopped.add(session.id)
                await run_in_threadpool(self._stop, session)
            return await JSONResponse({"error": "That's a lot of mail in a short time. Enter a code from your "
                                                "authenticator app to go on.", "step_up": True},
                                      status_code=429)(scope, receive, send)
        self.stopped.discard(session.id)
        return await self.app(scope, receive, send)

    def _session(self, token):
        with self.conn() as c:
            return webauth.session_for(c, token)

    def _stop(self, session):
        with self.conn() as c:
            webauth.event(c, "bulk_stop", session.origin, budget=self.reads.budget)
            c.commit()
        if self.notify:
            self.notify("Talos: reading paused",
                        f"A session ({session.origin}) read {self.reads.budget} messages in "
                        f"{self.reads.window // 60} minutes. It needs an authenticator code to go on.")
