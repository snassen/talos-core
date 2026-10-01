"""Composing mail: drafts, the sending accounts, signatures, warnings and the MIME message.

Nothing here sends. Sending lives in talos.send alone, which only the web app's send route
reaches, and only with a confirmation token (CLAUDE.md, promise 1). This module prepares
what the compose pane shows and what talos.send would deliver:

- senders(): every account that could send, whether it can be picked now, and why not.
  The work account (Graph) needs the send permission (graphauth.SEND_SCOPES); the IMAP accounts
  need to be enabled and have their app password in the Keychain. Existence is checked
  without reading a secret.
- drafts, saved while the owner types; a reply or forward starts from its original.
- signatures, per account and per kind of mail (new, or replies and forwards), one default each.
- warnings: a reply sent from another account than the original came to, work mail from a
  personal account or personal mail from the work account, domains never seen before.
- outgoing() and build_mime(): the exact message, with a plain-text part and, when the
  signature has formatting, a simple HTML part.
"""

from __future__ import annotations

import hashlib
import html as htmlmod
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid

import psycopg
from psycopg.types.json import Jsonb

from talos import accounts, secrets

MODES = ("new", "reply", "reply_all", "forward")
SENDING_PROVIDERS = ("gmail", "graph", "imap")
MAX_RECIPIENTS = 50
MAX_BODY = 200_000  # characters; a mail the owner writes, not a document

# Where each IMAP account sends. The same app password as IMAP; an account's settings may
# override with smtp_host / smtp_port / smtp_security.
SMTP_FOR_IMAP = {
    "imap.mail.me.com": ("smtp.mail.me.com", 587, "starttls"),
    "mailcluster.loopia.se": ("mailcluster.loopia.se", 587, "starttls"),
}
GMAIL_SMTP = ("smtp.gmail.com", 587, "starttls")

# Which side of the owner's life an account belongs to, for the mismatch warnings: each account's
# "sphere" in accounts.json, "work" or "personal". An account with neither (an association's, say)
# never warns.
ACCOUNT_SPHERE = {a["id"]: a["sphere"] for a in accounts.ACCOUNTS if a.get("sphere") in ("work", "personal")}
CONSUMER_DOMAINS = {"gmail.com", "googlemail.com", "hotmail.com", "hotmail.se", "outlook.com", "live.com",
                    "live.se", "msn.com", "icloud.com", "me.com", "mac.com", "yahoo.com", "yahoo.se",
                    "telia.com", "bredband.net", "spray.se", "proton.me", "protonmail.com"}

ADDRESS = re.compile(r"^[^@\s<>,;\"]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")
PREFIX = re.compile(r"^\s*(re|sv|vs|fw|fwd|vb|aw|wg)\s*:\s*", re.I)


class ComposeError(ValueError):
    pass


# ---------------------------------------------------------------- the sending accounts

class Probe:
    """What the sending accounts have, asked without reading a secret (tests pass a fake).

    secret_exists: the Keychain item is there (no access dialog).
    graph_can_send: the Microsoft sign-in carries the send scope; asked silently, remembered a minute."""

    def __init__(self, ttl: float = 60.0):
        self.ttl = ttl
        self._seen: dict[str, tuple[float, bool]] = {}

    def _remember(self, key: str, fn) -> bool:
        hit = self._seen.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        value = bool(fn())
        self._seen[key] = (time.monotonic(), value)
        return value

    def secret_exists(self, key: str) -> bool:
        return self._remember("secret:" + key, lambda: secrets.exists(key))

    def graph_can_send(self, account: dict) -> bool:
        def ask() -> bool:
            if not secrets.exists(f"graph-token-cache:{account['id']}"):
                return False
            try:
                from talos import graphauth
                s = account["settings"]
                auth = graphauth.GraphAuth(account["id"], s["tenant_id"], s["client_id"])
                return all(auth.granted(scope) for scope in graphauth.SEND_SCOPES)
            except Exception:  # any doubt about the permission: not pickable
                return False
        return self._remember("graph:" + account["id"], ask)


def smtp_server(account: dict) -> tuple[str, int, str] | None:
    """(host, port, security) an account sends through, or None for Graph and unknown hosts."""
    s = account.get("settings") or {}
    if s.get("smtp_host"):
        return s["smtp_host"], int(s.get("smtp_port", 587)), s.get("smtp_security", "starttls")
    if account["provider"] == "gmail":
        return GMAIL_SMTP
    if account["provider"] == "imap":
        return SMTP_FOR_IMAP.get(s.get("host", ""))
    return None


def send_permission() -> str | None:
    """The Graph permission sending needs, by the name graphauth gives it (SEND_SCOPES)."""
    try:
        from talos.graphauth import SEND_SCOPES
        return " and ".join(SEND_SCOPES)
    except ImportError:
        return None


def graph_how(account_id: str) -> str:
    p = send_permission()
    return (f"The Microsoft 365 account needs the Graph permission {p or 'to send mail'} before Talos can send from it. In the"
            f" Entra admin center, open App registrations → Talos (local) → API permissions → Add a permission →"
            f" Microsoft Graph → Delegated permissions → {p or 'the send permission'}, then Grant admin consent."
            f" Then sign in once more at the keyboard (it asks for the new permission): uv run talos auth graph {account_id}")


def senders(conn: psycopg.Connection, probe: Probe) -> list[dict]:
    """Every account that could send: {id, name, address, provider, pickable, status, why, how, default}.

    Pickable means the account is enabled and has what sending needs. Teams and imported files
    never send, so they are not listed."""
    default = get_setting(conn, "default_account")
    out = []
    for a in conn.execute("select * from account where provider = any(%s) order by id",
                          (list(SENDING_PROVIDERS),)).fetchall():
        s = a["settings"] or {}
        row = {"id": a["id"], "name": a["display_name"] or a["id"], "address": a["address"],
               "provider": a["provider"], "pickable": False, "status": "ready", "why": None, "how": None,
               "default": a["id"] == default}
        if not a["enabled"]:
            row.update(status="off", why=f"{row['name']} is not enabled in Talos.",
                       how=f"Enable the account first (it is off in the account table), then add its app password:"
                           f" security add-generic-password -s talos -a {s.get('secret', '<key>')} -w")
        elif a["provider"] == "graph":
            if not probe.graph_can_send(a):
                row.update(status="needs permission",
                           why=f"The Microsoft sign-in does not carry {send_permission() or 'the send permission'}.",
                           how=graph_how(a["id"]))
        elif not smtp_server(a):
            row.update(status="no server", why="Talos does not know this account's SMTP server.",
                       how="Set smtp_host (and smtp_port) in the account's settings.")
        elif not s.get("secret") or not probe.secret_exists(s["secret"]):
            row.update(status="no password", why="Its app password is not in the Keychain.",
                       how=f"security add-generic-password -s talos -a {s.get('secret', '<key>')} -w")
        row["pickable"] = row["status"] == "ready"
        out.append(row)
    order = {a["id"]: i for i, a in enumerate(accounts.ui())}   # the web's order of accounts
    return sorted(out, key=lambda r: (order.get(r["id"], 99), r["id"]))


def pickable(conn: psycopg.Connection, probe: Probe, account_id: str | None) -> dict:
    """The sender row for account_id, or a ComposeError saying why it cannot send."""
    if not account_id:
        raise ComposeError("choose the account to send from")
    for r in senders(conn, probe):
        if r["id"] == account_id:
            if not r["pickable"]:
                raise ComposeError(f"{r['name']} cannot send: {r['status']}. {r['why'] or ''}".strip())
            return r
    raise ComposeError(f"{account_id} is not an account that can send")


# ---------------------------------------------------------------- settings

def get_setting(conn: psycopg.Connection, key: str, default=None):
    row = conn.execute("select value from send_setting where key = %s", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: psycopg.Connection, key: str, value) -> None:
    if value is None:
        conn.execute("delete from send_setting where key = %s", (key,))
        return
    conn.execute("insert into send_setting (key, value) values (%s, %s) on conflict (key)"
                 " do update set value = excluded.value, updated_at = now()", (key, Jsonb(value)))


def set_default_account(conn: psycopg.Connection, probe: Probe, account_id: str | None) -> None:
    """The account a new mail starts from. Even with a default, the From block is shown and Send
    is confirmed. None: no default, so every new mail starts with the choice."""
    if account_id is not None:
        pickable(conn, probe, account_id)
    set_setting(conn, "default_account", account_id)


def from_name(conn: psycopg.Connection) -> str | None:
    """The name recipients see: the owner's setting, else the name on most of their sent mail."""
    name = get_setting(conn, "from_name")
    if name:
        return str(name)
    row = conn.execute("select from_name, count(*) n from message where direction in ('out', 'self')"
                       " and coalesce(from_name, '') <> '' and medium = 'email'"
                       " group by 1 order by 2 desc limit 1").fetchone()
    return row["from_name"] if row else None


# ---------------------------------------------------------------- addresses

def parse_addresses(values) -> list[str]:
    """Addresses as the pane sends them (a list, or one comma-separated string) → 'Name <a@b>' or 'a@b'.
    A ComposeError names the first one that is not an address."""
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise ComposeError("addresses are a list of text")
    out, seen = [], set()
    for name, addr in getaddresses([v for v in values if v.strip()]):
        addr = addr.strip()
        if not addr:
            continue
        if not ADDRESS.match(addr):
            raise ComposeError(f"{addr!r} is not an e-mail address")
        key = addr.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(formataddr((name.strip(), addr)) if name.strip() else addr)
    return out


def formataddr(pair: tuple[str, str]) -> str:
    """'Name <a@b>' as the owner reads it (email.utils.formataddr would MIME-encode a name like Nyström);
    the header is encoded when the message is built. A name with specials is quoted."""
    name, addr = " ".join((pair[0] or "").split()), pair[1]
    if not name:
        return addr
    if re.search(r'[()<>@,;:\\".\[\]]', name):
        name = '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"{name} <{addr}>"


def bare(addr: str) -> str:
    return (getaddresses([addr])[0][1] or addr).strip().lower()


def domain(addr: str) -> str:
    return bare(addr).rsplit("@", 1)[-1]


def suggest(conn: psycopg.Connection, q: str, limit: int = 8) -> list[dict]:
    """Addresses for the To/Cc/Bcc autocomplete: people the owner has mailed first, by how often."""
    q = (q or "").strip().lower()
    if len(q) < 2:
        return []
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return conn.execute(
        "with cand as (select a.address, p.display_name as name from address a join person p on p.id = a.person_id"
        "              where a.address like %(like)s or p.display_name ilike %(like)s limit 300)"
        " select c.address, c.name,"
        "   (select count(*) from participant pa join message m on m.id = pa.message_id"
        "     where pa.address = c.address and pa.role in ('to', 'cc', 'bcc') and m.direction in ('out', 'self')"
        "       and m.medium = 'email') as sent,"
        "   (select count(*) from participant pa where pa.address = c.address and pa.role = 'from') as received"
        " from cand c order by 3 desc, 4 desc, c.address limit %(limit)s",
        {"like": like, "limit": limit}).fetchall()


# ---------------------------------------------------------------- signatures

def _signature_html(raw: str | None) -> str | None:
    """The owner's signature HTML through the reading pane's allow-list cleaner (no script, no forms,
    no event handlers), as a fragment. Remote images (a logo) are kept, over https."""
    if raw is None or not raw.strip():
        return None
    from talos import mailhtml
    doc = mailhtml.sanitize(raw, allow_remote=True).html
    m = re.search(r"<body[^>]*>(.*)</body>", doc, re.S | re.I)
    frag = (m.group(1) if m else doc).strip()
    return frag or None


def signatures(conn: psycopg.Connection) -> list[dict]:
    rows = conn.execute("select * from signature order by lower(name), id").fetchall()
    defaults = conn.execute("select * from signature_default").fetchall()
    for r in rows:
        r["defaults"] = sorted(f"{d['account_id']}:{d['mode']}" for d in defaults if d["signature_id"] == r["id"])
    return rows


def save_signature(conn: psycopg.Connection, body: dict, sig_id: int | None = None) -> dict:
    """Create or update a signature from the settings form: {name, body_text, body_html, accounts,
    for_new, for_reply, default_for: [account ids]}. default_for makes it the default for those
    accounts, for each kind of mail it is used for (one default per account per kind)."""
    name = str(body.get("name") or "").strip()
    if not name:
        raise ComposeError("a signature needs a name")
    text = str(body.get("body_text") or "").rstrip()
    html = _signature_html(body.get("body_html"))
    accounts = body.get("accounts") or []
    known = {r["id"] for r in conn.execute("select id from account where provider = any(%s)", (list(SENDING_PROVIDERS),))}
    if not isinstance(accounts, list) or not all(a in known for a in accounts):
        raise ComposeError(f"accounts are some of: {', '.join(sorted(known))}")
    if not accounts:
        raise ComposeError("choose at least one account the signature applies to")
    for_new, for_reply = bool(body.get("for_new", True)), bool(body.get("for_reply", True))
    if not for_new and not for_reply:
        raise ComposeError("use it for new mail, for replies and forwards, or both")
    if not text and not html:
        raise ComposeError("the signature is empty")
    if sig_id is None:
        sig_id = conn.execute("insert into signature (name, body_text, body_html, accounts, for_new, for_reply)"
                              " values (%s, %s, %s, %s, %s, %s) returning id",
                              (name, text, html, accounts, for_new, for_reply)).fetchone()["id"]
    else:
        if not conn.execute("update signature set name = %s, body_text = %s, body_html = %s, accounts = %s,"
                            " for_new = %s, for_reply = %s, updated_at = now() where id = %s returning id",
                            (name, text, html, accounts, for_new, for_reply, sig_id)).fetchone():
            raise ComposeError("no such signature")
    # Defaults that no longer hold (an account or a kind taken away) go.
    conn.execute("delete from signature_default where signature_id = %s and (not (account_id = any(%s))"
                 " or (mode = 'new' and not %s) or (mode = 'reply' and not %s))",
                 (sig_id, accounts, for_new, for_reply))
    default_for = body.get("default_for") or []
    if not isinstance(default_for, list) or not all(a in accounts for a in default_for):
        raise ComposeError("default_for names accounts the signature applies to")
    if "default_for" in body:  # the form says every account it is the default for
        conn.execute("delete from signature_default where signature_id = %s and not (account_id = any(%s))",
                     (sig_id, default_for))
    for acc in default_for:
        for mode, on in (("new", for_new), ("reply", for_reply)):
            if on:
                conn.execute("insert into signature_default (account_id, mode, signature_id) values (%s, %s, %s)"
                             " on conflict (account_id, mode) do update set signature_id = excluded.signature_id",
                             (acc, mode, sig_id))
    return next(r for r in signatures(conn) if r["id"] == sig_id)


def remove_signature(conn: psycopg.Connection, sig_id: int) -> bool:
    return conn.execute("delete from signature where id = %s returning id", (sig_id,)).fetchone() is not None


def kind_of(mode: str) -> str:
    """Signatures know two kinds of mail: new, and replies (which include forwards)."""
    return "new" if mode == "new" else "reply"


def applicable(conn: psycopg.Connection, account_id: str, mode: str) -> list[dict]:
    col = "for_new" if kind_of(mode) == "new" else "for_reply"
    return conn.execute(f"select * from signature where %s = any(accounts) and {col} order by lower(name), id",
                        (account_id,)).fetchall()


def signature_for(conn: psycopg.Connection, account_id: str | None, mode: str) -> dict | None:
    """The signature a mail starts with: the account's default for this kind of mail; with no
    default, the only signature that applies; else none."""
    if not account_id:
        return None
    options = applicable(conn, account_id, mode)
    d = conn.execute("select signature_id from signature_default where account_id = %s and mode = %s",
                     (account_id, kind_of(mode))).fetchone()
    if d:
        for s in options:
            if s["id"] == d["signature_id"]:
                return s
    return options[0] if len(options) == 1 else None


# ---------------------------------------------------------------- drafts

DRAFT_FIELDS = ("account_id", "to_addrs", "cc_addrs", "bcc_addrs", "subject", "body", "include_quote", "signature_id")


def get_draft(conn: psycopg.Connection, draft_id: int) -> dict | None:
    return conn.execute("select * from draft where id = %s", (draft_id,)).fetchone()


def drafts(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("select id, account_id, mode, subject, to_addrs, left(body, 140) as snippet, updated_at"
                        " from draft order by updated_at desc limit 100").fetchall()


def clean_subject(subject: str | None) -> str:
    t = (subject or "").strip()
    while True:
        n = PREFIX.sub("", t)
        if n == t:
            return t
        t = n


def _quote(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in (text or "").rstrip().splitlines())


def _when(m: dict) -> str:
    at = m.get("sent_at") or m.get("received_at")
    return at.astimezone().strftime("%Y-%m-%d %H:%M") if isinstance(at, datetime) else ""


def start(conn: psycopg.Connection, probe: Probe, mode: str = "new", original_id: int | None = None) -> dict:
    """A new draft: blank from the default account, or a reply, reply-all or forward of a message,
    from the account the original was received on."""
    if mode not in MODES:
        raise ComposeError(f"mode is one of {', '.join(MODES)}")
    fields: dict = {"mode": mode, "to_addrs": [], "cc_addrs": [], "subject": "", "quoted": "",
                    "in_reply_to": None, "references_ids": [], "original_id": None, "original_account_id": None}
    ready = {r["id"] for r in senders(conn, probe) if r["pickable"]}
    default = get_setting(conn, "default_account")
    account = default if default in ready else None
    if mode != "new":
        if original_id is None:
            raise ComposeError("a reply or forward needs the message it answers")
        m = conn.execute("select m.*, t.body_text from message m left join message_text t on t.message_id = m.id"
                         " where m.id = %s", (original_id,)).fetchone()
        if not m:
            raise ComposeError("no such message")
        if m["medium"] != "email":
            raise ComposeError("Teams messages are answered in Teams")
        parts = conn.execute("select role, address, name from participant where message_id = %s order by role, ordinal",
                             (original_id,)).fetchall()
        mine = {r["address"] for r in conn.execute("select address from my_address")}

        def people(*roles):
            return [formataddr((p["name"] or "", p["address"])) if p["name"] else p["address"]
                    for p in parts if p["role"] in roles]

        def not_me(xs):
            return [x for x in xs if bare(x) not in mine]

        sent_by_me = m["direction"] in ("out", "self")
        sender = people("reply_to") or people("from")
        if mode == "reply":
            fields["to_addrs"] = people("to") if sent_by_me else sender
        elif mode == "reply_all":
            if sent_by_me:
                fields["to_addrs"], fields["cc_addrs"] = people("to"), people("cc")
            else:
                fields["to_addrs"] = not_me(sender + people("to"))
                fields["cc_addrs"] = not_me(people("cc"))
        def usable(xs):  # an archive address that is not one (undisclosed-recipients:;) is left out
            out = []
            for x in xs:
                try:
                    out += [a for a in parse_addresses([x]) if bare(a) not in {bare(o) for o in out}]
                except ComposeError:
                    pass
            return out

        fields["to_addrs"] = usable(fields["to_addrs"])
        fields["cc_addrs"] = [c for c in usable(fields["cc_addrs"]) if bare(c) not in {bare(t) for t in fields["to_addrs"]}]
        subject = clean_subject(m["subject"])
        fields["subject"] = ("Fwd: " if mode == "forward" else "Re: ") + subject if subject else \
            ("Fwd:" if mode == "forward" else "Re:")
        who = formataddr((m["from_name"] or "", m["from_address"] or "")) if m["from_name"] else (m["from_address"] or "")
        if mode == "forward":
            head = ["---------- Forwarded message ----------", f"From: {who}", f"Date: {_when(m)}",
                    f"Subject: {m['subject'] or ''}"]
            if people("to"):
                head.append("To: " + ", ".join(people("to")))
            if people("cc"):
                head.append("Cc: " + ", ".join(people("cc")))
            fields["quoted"] = "\n".join(head) + "\n\n" + (m["body_text"] or "").rstrip()
        else:
            fields["quoted"] = f"On {_when(m)}, {who} wrote:\n" + _quote(m["body_text"] or "")
            if m["rfc_message_id"]:  # stored without the angle brackets the headers need
                fields["in_reply_to"] = f"<{m['rfc_message_id']}>"
                fields["references_ids"] = [f"<{r}>" for r in (m["references_ids"] or []) if r][-20:] + \
                    [fields["in_reply_to"]]
        fields["original_id"] = original_id
        fields["original_account_id"] = m["account_id"]
        # The account it came to, and never quietly another: when that one cannot send, the owner chooses.
        account = m["account_id"] if m["account_id"] in ready else None
    sig = signature_for(conn, account, mode)
    row = conn.execute(
        "insert into draft (account_id, mode, original_id, original_account_id, to_addrs, cc_addrs, subject, quoted,"
        " in_reply_to, references_ids, signature_id) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id",
        (account, mode, fields["original_id"], fields["original_account_id"], fields["to_addrs"], fields["cc_addrs"],
         fields["subject"], fields["quoted"], fields["in_reply_to"], fields["references_ids"],
         sig["id"] if sig else None)).fetchone()
    return get_draft(conn, row["id"])


def save_draft(conn: psycopg.Connection, draft_id: int, body: dict) -> dict:
    """Save what the pane holds. Only the fields the owner edits; addresses are checked as they are
    saved, but an unfinished one is kept as typed until they send (confirm checks them)."""
    d = get_draft(conn, draft_id)
    if not d:
        raise ComposeError("no such draft")
    unknown = sorted(set(body) - set(DRAFT_FIELDS) - {"token"})
    if unknown:
        raise ComposeError(f"cannot set {', '.join(unknown)}")
    sets, args = [], []
    for k in DRAFT_FIELDS:
        if k not in body:
            continue
        v = body[k]
        if k in ("to_addrs", "cc_addrs", "bcc_addrs"):
            if isinstance(v, str):
                v = [x.strip() for x in v.split(",")]
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise ComposeError(f"{k} is a list of addresses")
            v = [x.strip() for x in v if x.strip()][:MAX_RECIPIENTS * 2]
        elif k in ("subject", "body"):
            if not isinstance(v, str):
                raise ComposeError(f"{k} is text")
            if k == "subject":
                v = " ".join(v.split())[:998]
            elif len(v) > MAX_BODY:
                raise ComposeError("the text is too long for a mail")
        elif k == "include_quote":
            v = bool(v)
        elif k == "account_id":
            if v is not None and not conn.execute("select 1 from account where id = %s and provider = any(%s)",
                                                  (v, list(SENDING_PROVIDERS))).fetchone():
                raise ComposeError(f"{v} is not an account that can send")
        elif k == "signature_id":
            if v is not None:
                if isinstance(v, bool) or not isinstance(v, int) or not conn.execute(
                        "select 1 from signature where id = %s", (v,)).fetchone():
                    raise ComposeError("no such signature")
        sets.append(f"{k} = %s")
        args.append(v)
    if sets:
        conn.execute(f"update draft set {', '.join(sets)}, updated_at = now() where id = %s", (*args, draft_id))
    return get_draft(conn, draft_id)


def discard(conn: psycopg.Connection, draft_id: int) -> bool:
    """Throw a local draft away. It never left Talos."""
    return conn.execute("delete from draft where id = %s returning id", (draft_id,)).fetchone() is not None


# ---------------------------------------------------------------- the outgoing message

@dataclass
class Outgoing:
    draft_id: int
    account_id: str
    from_addr: str
    from_name: str | None
    to: list[str]
    cc: list[str]
    bcc: list[str]
    subject: str
    text: str
    html: str | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    attachments: int = 0

    @property
    def recipients(self) -> list[str]:
        return [bare(a) for a in self.to + self.cc + self.bcc]

    def summary(self) -> dict:
        """What the confirmation restates: never the body."""
        return {"account_id": self.account_id, "from_addr": self.from_addr, "from_name": self.from_name,
                "to": self.to, "cc": self.cc, "bcc": self.bcc, "subject": self.subject,
                "attachments": self.attachments, "reply": bool(self.in_reply_to),
                "characters": len(self.text)}


def _html_part(body: str, sig_html: str, sig_text: str, quoted: str) -> str:
    def para(text: str) -> str:
        return "<br>\n".join(htmlmod.escape(line) for line in text.split("\n"))
    parts = [f'<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;font-size:14px">{para(body)}</div>']
    parts.append(f'<div class="talos-signature" style="margin-top:14px">{sig_html}</div>' if sig_html else
                 (f"<div style=\"margin-top:14px\">{para(sig_text)}</div>" if sig_text else ""))
    if quoted:
        parts.append('<blockquote style="margin:14px 0 0;padding-left:10px;border-left:2px solid #ccc;color:#555">'
                     f"{para(quoted)}</blockquote>")
    return '<!doctype html><html><head><meta charset="utf-8"></head><body>' + "\n".join(parts) + "</body></html>"


def outgoing(conn: psycopg.Connection, probe: Probe, draft_id: int) -> Outgoing:
    """The exact mail the draft would send, checked: a pickable account, real addresses, at least
    one recipient. A ComposeError says what is missing."""
    d = get_draft(conn, draft_id)
    if not d:
        raise ComposeError("no such draft")
    acct = pickable(conn, probe, d["account_id"])
    to, cc, bcc = parse_addresses(d["to_addrs"]), parse_addresses(d["cc_addrs"]), parse_addresses(d["bcc_addrs"])
    if not (to or cc or bcc):
        raise ComposeError("add at least one recipient")
    if len(to) + len(cc) + len(bcc) > MAX_RECIPIENTS:
        raise ComposeError(f"at most {MAX_RECIPIENTS} recipients in one mail")
    body = d["body"].rstrip()
    sig = conn.execute("select * from signature where id = %s", (d["signature_id"],)).fetchone() \
        if d["signature_id"] else None
    sig_text = (sig["body_text"] or "").rstrip() if sig else ""
    quoted = d["quoted"].rstrip() if d["include_quote"] else ""
    text = body
    for extra in (sig_text, quoted):
        if extra:
            text += ("\n\n" if text else "") + extra
    html = _html_part(body, sig["body_html"], sig_text, quoted) if sig and sig["body_html"] else None
    return Outgoing(draft_id=d["id"], account_id=acct["id"], from_addr=acct["address"], from_name=from_name(conn),
                    to=to, cc=cc, bcc=bcc, subject=d["subject"], text=text + "\n", html=html,
                    in_reply_to=d["in_reply_to"], references=list(d["references_ids"] or []))


def fingerprint(out: Outgoing) -> str:
    """A digest of everything that decides what goes where: the draft, the account and its address,
    every recipient, the subject, the text and HTML, the threading headers, the attachment count.
    A confirmation is valid only for this exact digest."""
    h = hashlib.sha256()
    for part in (str(out.draft_id), out.account_id, out.from_addr.lower(), out.from_name or "",
                 "\x1f".join(out.to), "\x1f".join(out.cc), "\x1f".join(out.bcc), out.subject,
                 hashlib.sha256(out.text.encode()).hexdigest(),
                 hashlib.sha256((out.html or "").encode()).hexdigest(),
                 out.in_reply_to or "", "\x1f".join(out.references), str(out.attachments)):
        h.update(part.encode("utf-8"))
        h.update(b"\x1e")
    return h.hexdigest()


def build_mime(out: Outgoing, *, message_id: str | None = None, date: str | None = None) -> EmailMessage:
    """The RFC 5322 message: From, To, Cc (never Bcc: those go in the envelope only), Subject, Date,
    Message-ID, In-Reply-To and References for a reply; a text/plain part, and a text/html
    alternative when the signature has formatting."""
    msg = EmailMessage()
    msg["From"] = formataddr((out.from_name or "", out.from_addr)) if out.from_name else out.from_addr
    if out.to:
        msg["To"] = ", ".join(out.to)
    if out.cc:
        msg["Cc"] = ", ".join(out.cc)
    msg["Subject"] = out.subject
    msg["Date"] = date or formatdate(localtime=True)
    msg["Message-ID"] = message_id or make_msgid(domain=out.from_addr.rsplit("@", 1)[-1])
    if out.in_reply_to:
        msg["In-Reply-To"] = out.in_reply_to
        msg["References"] = " ".join(dict.fromkeys(out.references or [out.in_reply_to]))
    # Quoted-printable for anything but ASCII, so the mail is 7-bit clean whatever the server offers.
    msg.set_content(out.text, cte=None if out.text.isascii() else "quoted-printable")
    if out.html:
        msg.add_alternative(out.html, subtype="html", cte="quoted-printable")
    return msg


# ---------------------------------------------------------------- warnings

def _original_sphere(conn: psycopg.Connection, message_id: int | None) -> str | None:
    if not message_id:
        return None
    rows = conn.execute("select dimension_id, value from effective_message_assignment where message_id = %s"
                        " and dimension_id in ('sphere', 'topic')", (message_id,)).fetchall()
    for r in rows:
        if r["dimension_id"] == "sphere" and r["value"] in ("work", "personal"):
            return r["value"]
    for r in rows:
        if r["dimension_id"] == "topic" and str(r["value"]).lower().startswith("work"):
            return "work"
    return None


def warnings(conn: psycopg.Connection, d: dict) -> list[dict]:
    """What the pane and the confirmation point out: {level: 'warn' | 'note', code, text}. None of
    them stops a send; the confirmation restates them."""
    out: list[dict] = []
    acc = d.get("account_id")
    names = {r["id"]: (r["display_name"] or r["id"], r["address"]) for r in conn.execute(
        "select id, display_name, address from account")}
    if d.get("mode") != "new" and d.get("original_account_id") and acc and acc != d["original_account_id"]:
        o_name, o_addr = names.get(d["original_account_id"], (d["original_account_id"], ""))
        n_name, n_addr = names.get(acc, (acc, ""))
        out.append({"level": "warn", "code": "reply_account",
                    "text": f"The original came to {o_name} ({o_addr}). You are answering from {n_name} ({n_addr})."})
    addrs = []
    for k in ("to_addrs", "cc_addrs", "bcc_addrs"):
        for x in d.get(k) or []:
            b = bare(x)
            if ADDRESS.match(b):
                addrs.append(b)
    mine = {r["address"]: r["account_id"] for r in conn.execute("select address, account_id from my_address")}
    work_domains = {a.rsplit("@", 1)[-1] for a, acc_id in mine.items() if ACCOUNT_SPHERE.get(acc_id or "") == "work"}
    others = [a for a in addrs if a not in mine]
    sphere = ACCOUNT_SPHERE.get(acc or "")
    orig = _original_sphere(conn, d.get("original_id"))
    if sphere == "personal":
        work_rcpt = [a for a in others if a.rsplit("@", 1)[-1] in work_domains]
        if work_rcpt or orig == "work":
            why = f"{', '.join(work_rcpt[:3])} is a work address" if work_rcpt else "the original is work mail"
            out.append({"level": "warn", "code": "work_from_personal",
                        "text": f"This looks like work mail ({why}), and it goes from {names.get(acc, (acc, ''))[0]}."})
    elif sphere == "work" and (orig == "personal" or (others and all(a.rsplit("@", 1)[-1] in CONSUMER_DOMAINS
                                                                      for a in others))):
        if orig == "personal" or not conn.execute(
                "select 1 from participant p join message m on m.id = p.message_id where p.address = any(%s)"
                " and m.account_id = %s and m.direction in ('out', 'self') limit 1", (others, acc)).fetchone():
            why = "the original is personal mail" if orig == "personal" else "every recipient is at a personal mail service"
            out.append({"level": "warn", "code": "personal_from_work",
                        "text": f"This looks like personal mail ({why}), and it goes from {names.get(acc, (acc, ''))[0]}."})
    domains = sorted({a.rsplit("@", 1)[-1] for a in others})
    if domains:
        seen = {r["domain"] for r in conn.execute("select distinct domain from address where domain = any(%s)", (domains,))}
        new = [x for x in domains if x not in seen]
        if new:
            out.append({"level": "note", "code": "new_domain",
                        "text": f"Never mailed with before: {', '.join(new)}. Check the spelling."})
    if not (d.get("subject") or "").strip():
        out.append({"level": "note", "code": "no_subject", "text": "There is no subject."})
    return out
