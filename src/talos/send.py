"""Sending a mail or a Teams post the owner composed and confirmed. The only module in Talos that can send.

The promise (CLAUDE.md, promise 1): Talos sends a mail only when the owner presses Send on a
mail they composed, after confirming the sending account. No AI,
rule, job or automation ever sends mail, and no API endpoint can send without a fresh human
confirmation. The build holds it (tests/test_guards.py):

- SMTP and Graph sendMail appear in this file and nowhere else;
- only the web app imports this module, for its two send routes (a mail, a Teams post): no CLI
  command, sync, rule run, Argus timer or model step can reach it (a guard imports every other
  module and checks);
- sending needs a confirmation token, and tokens exist only inside the Confirmations object
  the web app creates for itself. There is no module-level book to borrow.

A confirmation token is issued by the confirm request the compose pane makes when the owner
presses Send, after the pane has shown them the From address in large type. It names one draft and the
digest of the exact message (compose.fingerprint: the account, its address, every recipient,
the subject, the text, the threading headers and the attachment count), is valid for
TOKEN_TTL seconds, only after MIN_DWELL seconds (a person reads before pressing "Yes"), and
only once. Any change after confirming, even one character, gives a different digest and the
send is refused: the owner confirms again.

A send is also refused when RATE_LIMIT sends were made in the past hour, as a safety net.
Every attempt, sent, failed or refused, is written to send_log, without the body.

The sent copy: Gmail files SMTP-sent mail in Sent by itself, and Graph saves it to Sent Items;
the next sync ingests it. Talos never appends it.

Teams: a post in a chat, a new post in a channel, or a reply to a channel thread. Pressing Enter (or Send)
in the conversation's own box is the confirmation, since the box belongs to one conversation and the owner
knows where they are writing: the page asks for a token for exactly that target and text (teams_fingerprint)
and redeems it at once, with no dwell, so the send route still cannot post on its own and a text changed in
between is refused. Teams has its own hourly limit (TEAMS_RATE_LIMIT), as a chat runs to more messages than
mail. Plain text only: no @mentions, no attachments, no edits or deletions. The ChannelMessage.Send and
ChatMessage.Send scopes are named here and in graphauth.py only.
"""

from __future__ import annotations

import base64
import secrets as pysecrets
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from typing import Callable, Protocol

import httpx
import psycopg

from talos import compose, personal, secrets

TOKEN_TTL = 300.0      # seconds a confirmation is valid
MIN_DWELL = 1.0        # seconds between the confirmation and "Yes, send"
RATE_LIMIT = 20        # mails sent (or failed) in RATE_WINDOW
TEAMS_RATE_LIMIT = 120 # Teams posts sent (or failed) in RATE_WINDOW
RATE_WINDOW = "1 hour"
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
GRAPH_SEND_URL = "https://graph.microsoft.com/v1.0/me/sendMail"


class SendRefused(Exception):
    """The send was not attempted: no or a stale confirmation, a change, the rate limit."""

    def __init__(self, code: str, text: str):
        super().__init__(text)
        self.code = code


class SendFailed(Exception):
    """The server did not take the mail."""


# ---------------------------------------------------------------- confirmations

@dataclass
class _Issued:
    draft_id: int | str   # a draft's id, or a Teams target's key ('teams:chat:<id>', 'teams:channel:…')
    digest: str
    issued_at: float
    dwell: float


class Confirmations:
    """One-time confirmation tokens, held in memory by the web app that issued them.

    A token is 256 random bits; the book keeps what it was issued for. A token from another
    book (another process, a script, a test that made its own) is unknown here."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, ttl: float = TOKEN_TTL,
                 dwell: float = MIN_DWELL):
        self.clock, self.ttl, self.dwell = clock, ttl, dwell
        self._issued: dict[str, _Issued] = {}

    def issue(self, draft_id: int | str, digest: str, *, dwell: float | None = None) -> str:
        """dwell: the seconds before the token can be used; the book's own unless given (a Teams post: 0)."""
        now = self.clock()
        # A new confirmation replaces the draft's earlier ones, and old tokens are swept.
        self._issued = {k: v for k, v in self._issued.items() if v.draft_id != draft_id and now - v.issued_at < self.ttl}
        token = pysecrets.token_urlsafe(32)
        self._issued[token] = _Issued(draft_id, digest, now, self.dwell if dwell is None else dwell)
        return token

    def redeem(self, token: str | None, draft_id: int | str, digest: str) -> None:
        """Use the token up, or raise SendRefused. A token is gone after one try, right or wrong."""
        if not token or not isinstance(token, str):
            raise SendRefused("no_confirmation", "Sending needs a confirmation: press Send and confirm the account.")
        issued = self._issued.pop(token, None)
        if issued is None:
            raise SendRefused("unknown_confirmation", "This confirmation is not known (used already, or from"
                                                      " another session). Press Send and confirm again.")
        age = self.clock() - issued.issued_at
        if issued.draft_id != draft_id:
            raise SendRefused("wrong_draft", "This confirmation is for another draft or conversation.")
        if age > self.ttl:
            raise SendRefused("expired", f"The confirmation expired after {int(self.ttl // 60)} minutes."
                                         " Press Send and confirm again.")
        if age < issued.dwell:
            raise SendRefused("too_fast", "Confirmed too quickly after it was asked. Press Send and confirm again.")
        if issued.digest != digest:
            raise SendRefused("changed", "It changed after you confirmed it. Press Send and confirm again.")


# ---------------------------------------------------------------- transports

class Transport(Protocol):
    def deliver(self, msg: EmailMessage, sender: str, recipients: list[str]) -> None: ...


class SmtpTransport:
    """SMTP with the account's app password, read from the Keychain at the moment of sending.

    security: 'starttls' (587), 'ssl' (465), or 'none', which is allowed only on the loopback
    (the tests' fake server): a password never crosses a network in the clear."""

    def __init__(self, host: str, port: int, security: str, username: str, password: Callable[[], str],
                 timeout: float = 30.0):
        if security not in ("starttls", "ssl", "none"):
            raise ValueError(f"unknown SMTP security {security!r}")
        if security == "none" and host not in LOOPBACK:
            raise ValueError("plain SMTP is allowed only on the loopback")
        self.host, self.port, self.security = host, port, security
        self.username, self.password, self.timeout = username, password, timeout

    def deliver(self, msg: EmailMessage, sender: str, recipients: list[str]) -> None:
        ctx = ssl.create_default_context()
        if self.security == "ssl":
            server = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=ctx)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        try:
            server.ehlo()
            if self.security == "starttls":
                server.starttls(context=ctx)
                server.ehlo()
            server.login(self.username, self.password())
            refused = server.send_message(msg, from_addr=sender, to_addrs=recipients)
            if refused:
                raise SendFailed("the server refused " + ", ".join(sorted(refused)))
        except smtplib.SMTPException as exc:
            raise SendFailed(f"{type(exc).__name__}: {exc}") from None
        finally:
            try:
                server.quit()
            except Exception:
                server.close()


class GraphTransport:
    """Microsoft Graph sendMail with the MIME message (base64), so the headers are the ones built
    here. Graph saves the copy in Sent Items. The token must carry Mail.Send."""

    def __init__(self, token: Callable[[], str], http: httpx.Client | None = None):
        self.token, self.http = token, http

    def deliver(self, msg: EmailMessage, sender: str, recipients: list[str]) -> None:
        data = base64.b64encode(msg.as_bytes(policy=SMTP_POLICY))
        client = self.http or httpx.Client(timeout=60)
        try:
            r = client.post(GRAPH_SEND_URL, content=data,
                            headers={"Authorization": f"Bearer {self.token()}", "Content-Type": "text/plain"})
        finally:
            if self.http is None:
                client.close()
        if r.status_code != 202:
            raise SendFailed(f"Graph answered {r.status_code}: {r.text[:300]}")


def transport_for(account: dict) -> Transport:
    """The real transport for an account row. Secrets are read only when a mail is delivered."""
    s = account.get("settings") or {}
    if account["provider"] == "graph":
        from talos.graphauth import SEND_SCOPES, GraphAuth  # Mail.Send, consented by the owner in Entra
        auth = GraphAuth(account["id"], s["tenant_id"], s["client_id"])
        return GraphTransport(lambda: auth.token(scopes=SEND_SCOPES))
    server = compose.smtp_server(account)
    if not server or not s.get("secret"):
        raise SendFailed(f"{account['id']} has no SMTP server or no Keychain item")
    host, port, security = server
    return SmtpTransport(host, port, security, s.get("username") or account["address"],
                         lambda: secrets.get(s["secret"]))


# ---------------------------------------------------------------- Teams

GRAPH = "https://graph.microsoft.com/v1.0"
TEAMS_TEXT_MAX = 20000


@dataclass
class TeamsPost:
    """Where a post goes and what it says. kind: 'chat' (a chat), 'channel' (a new post in a channel) or
    'reply' (a reply to a channel thread)."""
    kind: str
    text: str
    chat_id: str | None = None
    team_id: str | None = None
    channel_id: str | None = None
    root_id: str | None = None
    where: str = ""

    @property
    def key(self) -> str:
        return ":".join(["teams", self.kind, self.chat_id or "", self.team_id or "", self.channel_id or "", self.root_id or ""])

    @property
    def path(self) -> str:
        from urllib.parse import quote
        q = lambda s: quote(s or "", safe="")  # noqa: E731
        if self.kind == "chat":
            return f"/chats/{q(self.chat_id)}/messages"
        if self.kind == "channel":
            return f"/teams/{q(self.team_id)}/channels/{q(self.channel_id)}/messages"
        return f"/teams/{q(self.team_id)}/channels/{q(self.channel_id)}/messages/{q(self.root_id)}/replies"


def teams_fingerprint(post: TeamsPost) -> str:
    import hashlib
    return hashlib.sha256(f"{post.key}\n{post.text}".encode()).hexdigest()


def teams_post(conn: psycopg.Connection, *, thread_id: int | None = None, team: str | None = None,
               channel: str | None = None, text: str = "") -> TeamsPost:
    """The target from what Talos knows: a thread (a chat, or a channel thread: a reply), or a team and
    channel by name (a new post). The ids come from the synced messages, never from the request."""
    text = (text or "").strip()
    if not text:
        raise SendRefused("empty", "Write something first.")
    if len(text) > TEAMS_TEXT_MAX:
        raise SendRefused("too_long", "That is too long for one Teams message.")
    if thread_id:
        h = conn.execute(
            "select m.headers, t.subject from message m join thread t on t.id = m.thread_id where m.thread_id = %s"
            " and m.medium in ('teams_chat', 'teams_channel') order by m.received_at desc nulls last, m.id desc limit 1",
            (thread_id,)).fetchone()
        if not h:
            raise SendRefused("no_target", "That is not a Teams conversation Talos knows.")
        hd = h["headers"] or {}
        if hd.get("x-teams-kind") == "channel":
            if not (hd.get("x-teams-team-id") and hd.get("x-teams-channel-id") and hd.get("x-teams-root-id")):
                raise SendRefused("no_target", "Talos does not know where this channel thread lives.")
            return TeamsPost("reply", text, team_id=hd["x-teams-team-id"], channel_id=hd["x-teams-channel-id"],
                             root_id=hd["x-teams-root-id"],
                             where=f"{hd.get('x-teams-team', 'Team')} › {hd.get('x-teams-channel', 'Channel')}, a reply to “{(h['subject'] or 'the post')[:60]}”")
        if not hd.get("x-teams-chat-id"):
            raise SendRefused("no_target", "Talos does not know this chat's id.")
        return TeamsPost("chat", text, chat_id=hd["x-teams-chat-id"], where=f"the chat “{(h['subject'] or 'Chat')[:80]}”")
    if team and channel:
        hd = conn.execute(
            "select headers from message where medium = 'teams_channel' and headers->>'x-teams-team' = %s"
            " and headers->>'x-teams-channel' = %s order by received_at desc nulls last limit 1", (team, channel)).fetchone()
        if not hd:
            raise SendRefused("no_target", "Talos has not seen that channel yet.")
        hd = hd["headers"]
        return TeamsPost("channel", text, team_id=hd["x-teams-team-id"], channel_id=hd["x-teams-channel-id"],
                         where=f"{team} › {channel}, a new post")
    raise SendRefused("no_target", "Choose a chat or a channel.")


class TeamsTransport:
    """POST the post to Graph as plain text. The token must carry ChannelMessage.Send / ChatMessage.Send."""

    def __init__(self, token: Callable[[], str], http: httpx.Client | None = None):
        self.token, self.http = token, http

    def post(self, post: TeamsPost) -> dict:
        client = self.http or httpx.Client(timeout=60)
        try:
            r = client.post(GRAPH + post.path, json={"body": {"contentType": "text", "content": post.text}},
                            headers={"Authorization": f"Bearer {self.token()}"})
        finally:
            if self.http is None:
                client.close()
        if r.status_code not in (200, 201):
            raise SendFailed(f"Teams answered {r.status_code}: {r.text[:300]}")
        return r.json()


def teams_transport_for(conn: psycopg.Connection) -> TeamsTransport:
    from talos.graphauth import TEAMS_SEND_SCOPES, GraphAuth  # consented by the owner in Entra
    acct = conn.execute("select * from account where id = 'teams'").fetchone()
    token_account = (acct["settings"] or {}).get("token_account") if acct else None
    if not token_account:
        raise SendFailed("the Teams account names no token_account (the Microsoft 365 account it signs in as)")
    s = conn.execute("select settings from account where id = %s", (token_account,)).fetchone()["settings"]
    auth = GraphAuth(token_account, s["tenant_id"], s["client_id"])
    return TeamsTransport(lambda: auth.token(scopes=TEAMS_SEND_SCOPES))


# ---------------------------------------------------------------- the send

def sends_in_window(conn: psycopg.Connection, *, teams: bool = False) -> int:
    """Mails, or Teams posts, sent or failed in the past RATE_WINDOW. Each has its own limit."""
    return conn.execute(f"select count(*) n from send_log where result in ('sent', 'failed')"
                        f" and at > now() - interval '{RATE_WINDOW}'"
                        f" and (coalesce(account_id, '') = 'teams') = %s", (teams,)).fetchone()["n"]


def check_rate(conn: psycopg.Connection, *, teams: bool = False) -> None:
    if teams:
        if sends_in_window(conn, teams=True) >= TEAMS_RATE_LIMIT:
            raise SendRefused("rate_limit", f"{TEAMS_RATE_LIMIT} Teams messages were posted in the past hour, the most"
                                            " Talos posts in an hour. Wait a while, or write in Teams itself.")
        return
    if sends_in_window(conn) >= RATE_LIMIT:
        raise SendRefused("rate_limit", f"{RATE_LIMIT} mails were sent in the past hour, the most Talos sends in an"
                                        " hour. Wait a while, or send from the mail app.")


def log(conn: psycopg.Connection, result: str, *, out: compose.Outgoing | None = None, draft: dict | None = None,
        message_id: str | None = None, detail: str | None = None) -> int:
    """One row per attempt. The body is never written: only who, when, from, to, subject, Message-ID."""
    if out is not None:
        row = (out.account_id, out.from_addr, [compose.bare(a) for a in out.to], [compose.bare(a) for a in out.cc],
               [compose.bare(a) for a in out.bcc], out.subject, out.in_reply_to, out.draft_id)
    else:
        d = draft or {}
        row = (d.get("account_id"), None, [], [], [], d.get("subject"), d.get("in_reply_to"), d.get("id"))
    return conn.execute(
        "insert into send_log (account_id, from_addr, to_addrs, cc_addrs, bcc_addrs, subject, in_reply_to, draft_id,"
        " message_id, result, detail, by_whom) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id",
        (*row, message_id, result, (detail or "")[:500] or None, personal.OWNER_ID)).fetchone()["id"]


class Sender:
    """What the web app's compose routes use: confirm, then send. Created once per app.

    transport_for_account and probe are replaced in tests and in the demo (a fake SMTP server);
    the default reads the Keychain, only when a confirmed mail is delivered."""

    def __init__(self, probe: compose.Probe, *, transport_for_account: Callable[[dict], Transport] = transport_for,
                 book: Confirmations | None = None, teams_transport: Callable[[psycopg.Connection], TeamsTransport] = teams_transport_for):
        self.probe = probe
        self.transport_for_account = transport_for_account
        self.teams_transport = teams_transport
        self.book = book or Confirmations()

    def teams_confirm(self, conn: psycopg.Connection, **target) -> dict:
        """The token for a Teams post: exactly this target and text, usable at once (the owner's Enter is the confirmation)."""
        post = teams_post(conn, **target)
        check_rate(conn, teams=True)
        token = self.book.issue(post.key, teams_fingerprint(post), dwell=0.0)
        return {"token": token, "expires_in": int(self.book.ttl), "wait": 0, "where": post.where,
                "kind": post.kind, "text": post.text}

    def teams_send(self, conn: psycopg.Connection, token: str | None, **target) -> dict:
        """Post it, if the token confirms exactly this target and text. Commits the log (no text in it)."""
        try:
            post = teams_post(conn, **target)
            self.book.redeem(token, post.key, teams_fingerprint(post))
            check_rate(conn, teams=True)
        except SendRefused as exc:
            log(conn, "refused", draft={"account_id": "teams", "subject": "Teams post"}, detail=f"{exc.code}: {exc}")
            conn.commit()
            raise
        try:
            got = self.teams_transport(conn).post(post)
        except Exception as exc:
            log(conn, "failed", draft={"account_id": "teams", "subject": post.where}, detail=f"{type(exc).__name__}: {exc}")
            conn.commit()
            raise SendFailed(str(exc)) from None
        log_id = log(conn, "sent", draft={"account_id": "teams", "subject": post.where}, message_id=got.get("id"))
        conn.commit()
        return {"sent": True, "log_id": log_id, "where": post.where, "message_id": got.get("id"),
                "post": {"kind": post.kind, "team_id": post.team_id, "channel_id": post.channel_id}}

    def confirm(self, conn: psycopg.Connection, draft_id: int) -> dict:
        """The confirmation step: the checked message's summary, the warnings and a token for it."""
        out = compose.outgoing(conn, self.probe, draft_id)
        check_rate(conn)
        token = self.book.issue(draft_id, compose.fingerprint(out))
        d = compose.get_draft(conn, draft_id)
        return {"token": token, "expires_in": int(self.book.ttl), "wait": self.book.dwell,
                "summary": out.summary(), "warnings": compose.warnings(conn, d)}

    def send(self, conn: psycopg.Connection, draft_id: int, token: str | None) -> dict:
        """Send the draft as it is now, if the token confirms exactly this message. Commits the log."""
        d = compose.get_draft(conn, draft_id)
        if not d:
            raise compose.ComposeError("no such draft")
        try:
            out = compose.outgoing(conn, self.probe, draft_id)
            self.book.redeem(token, draft_id, compose.fingerprint(out))
            check_rate(conn)
        except SendRefused as exc:
            log(conn, "refused", draft=d, detail=f"{exc.code}: {exc}")
            conn.commit()
            raise
        acct = conn.execute("select * from account where id = %s", (out.account_id,)).fetchone()
        msg = compose.build_mime(out)
        mid = msg["Message-ID"]
        try:
            self.transport_for_account(acct).deliver(msg, out.from_addr, out.recipients)
        except Exception as exc:
            log(conn, "failed", out=out, message_id=mid, detail=f"{type(exc).__name__}: {exc}")
            conn.commit()
            raise SendFailed(str(exc)) from None
        log_id = log(conn, "sent", out=out, message_id=mid)
        compose.discard(conn, draft_id)  # the sent copy comes back through Sent at the next sync
        conn.commit()
        return {"sent": True, "log_id": log_id, "message_id": mid, "from_addr": out.from_addr,
                "to": out.to, "cc": out.cc, "bcc": out.bcc, "subject": out.subject}
