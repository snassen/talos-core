"""Signing in to Talos Web (docs/security.md).

Talos Web holds all of the owner's mail, so no request reaches it without a session, and a session
takes two things: the password and a code from the authenticator app on the owner's phone (TOTP, RFC 6238).
The rules:

- **Credentials live in the Keychain**, written only by ``talos web setup`` in the owner's own Terminal:
  the password as a scrypt hash (never the password), the authenticator secret, and the hashes of
  eight one-time recovery codes. A copy of the database holds none of them.
- **A session** is a random token in an HttpOnly, SameSite=Strict cookie; the database keeps only its
  SHA-256. It ends after IDLE of no use and MAX_AGE at the most, and can be ended from the page or
  the command line (all at once, too).
- **Wrong attempts lock the door**: FAIL_LIMIT failures within LOCK_WINDOW lock signing in for the
  rest of the window. A code is accepted once: its time step is remembered.
- **No easy bulk copy**: a session may read READ_BUDGET messages, conversations or attachments per
  READ_WINDOW. Beyond it the reads stop until the owner enters an authenticator code again (step-up), so a
  stolen session cannot quietly pull the archive out.
- Everything is logged in web_event, and sign-ins, lockouts and bulk stops become macOS
  notifications.

The web app enforces this in every environment except tests (see web.app.create).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets as pysecrets
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

PASSWORD_KEY, TOTP_KEY, RECOVERY_KEY = "web:password", "web:totp", "web:recovery"
IDLE = timedelta(hours=24)
MAX_AGE = timedelta(days=7)
FAIL_LIMIT, LOCK_WINDOW = 5, timedelta(minutes=15)
READ_BUDGET, READ_WINDOW = 400, 600  # reads per session per seconds
MIN_PASSWORD = 12
COOKIE = "talos_session"
TOTP_STEP, TOTP_DIGITS = 30, 6
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}


class AuthError(ValueError):
    pass


# ---------------------------------------------------------------- credentials

def hash_password(password: str) -> str:
    salt = pysecrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, dklen=32, maxmem=2 ** 26, **SCRYPT)
    return "scrypt${n}${r}${p}$".format(**SCRYPT) + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def check_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, dk = stored.split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=32, maxmem=2 ** 26)
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def new_totp_secret() -> str:
    return base64.b32encode(pysecrets.token_bytes(20)).decode().rstrip("=")


def totp_at(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    return str((struct.unpack(">I", digest[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** TOTP_DIGITS).zfill(TOTP_DIGITS)


def totp_step(code: str, secret: str, *, now: float | None = None) -> int | None:
    """The time step the code belongs to (this one, or one either side for clock drift), or None."""
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if len(code) != TOTP_DIGITS:
        return None
    t = int((now if now is not None else time.time()) // TOTP_STEP)
    for step in (t - 1, t, t + 1):
        if hmac.compare_digest(totp_at(secret, step), code):
            return step
    return None


def otpauth_uri(secret: str, account: str | None = None, issuer: str = "Talos") -> str:
    """The authenticator app's enrolment link; account is its label there (default: the owner's name, owner.json)."""
    if account is None:
        from talos import personal
        account = personal.owner()["name"]
    return f"otpauth://totp/{issuer}:{account}?secret={secret}&issuer={issuer}&digits={TOTP_DIGITS}&period={TOTP_STEP}"


def new_recovery_codes(n: int = 8) -> list[str]:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return ["-".join("".join(pysecrets.choice(alphabet) for _ in range(4)) for _ in range(3)) for _ in range(n)]


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.strip().lower().replace(" ", "").encode()).hexdigest()


class KeychainStore:
    """The credentials in the Keychain (talos.secrets); tests pass a dict-backed store instead."""

    def get(self, key: str) -> str | None:
        from talos import secrets
        return secrets.get_optional(key)

    def put(self, key: str, value: str) -> None:
        from talos import secrets
        secrets.put(key, value)


class MemoryStore(dict):
    def put(self, key, value):
        self[key] = value


def setup(store, password: str, totp_secret: str, recovery_codes: list[str]) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"the password needs at least {MIN_PASSWORD} characters")
    store.put(PASSWORD_KEY, hash_password(password))
    store.put(TOTP_KEY, totp_secret)
    store.put(RECOVERY_KEY, json.dumps([_code_hash(c) for c in recovery_codes]))


def is_set_up(store) -> bool:
    return bool(store.get(PASSWORD_KEY) and store.get(TOTP_KEY))


# ---------------------------------------------------------------- events and sessions

def event(conn: psycopg.Connection, kind: str, origin: str = "", **detail) -> None:
    conn.execute("insert into web_event (kind, origin, detail) values (%s, %s, %s)", (kind, origin, Jsonb(detail)))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def locked_until(conn: psycopg.Connection, *, now: datetime | None = None) -> datetime | None:
    """When signing in opens again, if too many attempts failed within the lock window."""
    now = now or _now()
    rows = conn.execute("select at from web_event where kind = 'login_fail' and at > %s"
                        " and at > coalesce((select max(at) from web_event where kind = 'login_ok'), '-infinity')"
                        " order by at", (now - LOCK_WINDOW,)).fetchall()
    if len(rows) >= FAIL_LIMIT:
        return rows[-FAIL_LIMIT]["at"] + LOCK_WINDOW
    return None


def _check_code(conn, store, code: str, *, now: float | None = None) -> str | None:
    """'totp' or 'recovery' when the code is good (and unused), else None. Uses it up."""
    secret = store.get(TOTP_KEY)
    step = totp_step(code, secret, now=now) if secret else None
    if step is not None:
        last = conn.execute("select value from web_state where key = 'totp_last_step'").fetchone()
        if last and int(last["value"]) >= step:
            return None  # this code (or a later one) was used already
        conn.execute("insert into web_state (key, value) values ('totp_last_step', %s)"
                     " on conflict (key) do update set value = excluded.value", (str(step),))
        return "totp"
    stored = json.loads(store.get(RECOVERY_KEY) or "[]")
    h = _code_hash(code)
    if h in stored:
        stored.remove(h)
        store.put(RECOVERY_KEY, json.dumps(stored))
        return "recovery"
    return None


@dataclass
class Session:
    id: str
    origin: str
    created_at: datetime
    expires_at: datetime


def sign_in(conn: psycopg.Connection, store, password: str, code: str, *, origin: str, user_agent: str = "",
            now: float | None = None) -> str:
    """A new session token for a right password and code; AuthError otherwise (and it is counted)."""
    if not is_set_up(store):
        raise AuthError("signing in is not set up yet: run  uv run talos web setup  in Terminal on this Mac")
    until = locked_until(conn)
    if until:
        raise AuthError(f"too many wrong attempts: signing in opens again at {until.astimezone():%H:%M}")
    ok_pw = check_password(password or "", store.get(PASSWORD_KEY))
    how = _check_code(conn, store, code or "", now=now) if ok_pw else None
    if not (ok_pw and how):
        event(conn, "login_fail", origin, reason="password" if not ok_pw else "code", agent=user_agent[:200])
        conn.commit()
        until = locked_until(conn)
        raise AuthError("locked: too many wrong attempts" if until else "wrong password or code")
    token = pysecrets.token_urlsafe(32)
    t = _now()
    conn.execute("insert into web_session (id, origin, user_agent, created_at, last_seen, expires_at)"
                 " values (%s, %s, %s, %s, %s, %s)",
                 (_token_id(token), origin, user_agent[:300], t, t, t + MAX_AGE))
    event(conn, "login_ok", origin, via=how, agent=user_agent[:200])
    if how == "recovery":
        left = len(json.loads(store.get(RECOVERY_KEY) or "[]"))
        event(conn, "recovery_used", origin, left=left)
    conn.commit()
    return token


def mint(conn: psycopg.Connection, *, minutes: int, origin: str) -> str:
    """A short session for a tool on this Mac itself (talos web session): logged and announced."""
    token = pysecrets.token_urlsafe(32)
    t = _now()
    conn.execute("insert into web_session (id, origin, user_agent, created_at, last_seen, expires_at)"
                 " values (%s, %s, 'talos web session', %s, %s, %s)",
                 (_token_id(token), origin, t, t, t + timedelta(minutes=max(1, min(minutes, 120)))))
    event(conn, "session_minted", origin, minutes=minutes)
    conn.commit()
    return token


def session_for(conn: psycopg.Connection, token: str | None) -> Session | None:
    """The live session for a cookie, touching last_seen at most once a minute."""
    if not token:
        return None
    t = _now()
    row = conn.execute("select * from web_session where id = %s and revoked_at is null and expires_at > %s"
                       " and last_seen > %s", (_token_id(token), t, t - IDLE)).fetchone()
    if not row:
        return None
    if t - row["last_seen"] > timedelta(minutes=1):
        conn.execute("update web_session set last_seen = %s where id = %s", (t, row["id"]))
        conn.commit()
    return Session(row["id"], row["origin"], row["created_at"], row["expires_at"])


def sign_out(conn: psycopg.Connection, token: str | None, *, everywhere: bool = False, keep: str | None = None) -> int:
    if everywhere:
        n = conn.execute("update web_session set revoked_at = now() where revoked_at is null and id <> %s",
                         (keep or "",)).rowcount
        event(conn, "signout_all", "", ended=n)
    else:
        n = conn.execute("update web_session set revoked_at = now() where id = %s and revoked_at is null",
                         (_token_id(token or ""),)).rowcount
        event(conn, "logout", "")
    conn.commit()
    return n


def sessions(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("select id, origin, user_agent, created_at, last_seen, expires_at from web_session"
                        " where revoked_at is null and expires_at > now() and last_seen > now() - %s"
                        " order by last_seen desc", (IDLE,)).fetchall()


def recent_events(conn: psycopg.Connection, limit: int = 30) -> list[dict]:
    return conn.execute("select at, kind, origin, detail from web_event order by at desc limit %s", (limit,)).fetchall()


def step_up(conn: psycopg.Connection, store, session: Session, code: str, reads) -> None:
    if not _check_code(conn, store, code):
        event(conn, "step_up_fail", session.origin)
        conn.commit()
        raise AuthError("wrong code")
    reads.reset(session.id)
    event(conn, "step_up", session.origin)
    conn.commit()


# ---------------------------------------------------------------- the bulk-read guard

class ReadBudget:
    """Reads of whole messages, conversations and attachments per session, in a sliding window."""

    def __init__(self, budget: int = READ_BUDGET, window: float = READ_WINDOW, clock=time.monotonic):
        self.budget, self.window, self.clock = budget, window, clock
        self.seen: dict[str, deque] = {}
        self.lock = threading.Lock()

    def take(self, session_id: str) -> bool:
        """Count one read; False when the budget is spent (the read must not happen)."""
        t = self.clock()
        with self.lock:
            q = self.seen.setdefault(session_id, deque())
            while q and q[0] <= t - self.window:
                q.popleft()
            if len(q) >= self.budget:
                return False
            q.append(t)
            return True

    def reset(self, session_id: str) -> None:
        with self.lock:
            self.seen.pop(session_id, None)
