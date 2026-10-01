"""Teams messages → rows. The Teams counterpart of ingest.py's MIME path.

A Teams message arrives as Graph JSON (a chatMessage). Its original is that JSON,
stored in the vault as blob kind 'json', hashed over canonical bytes so the same
content always has the same name. The mapping reuses the Ingestor's helpers, so a
Teams sender is the same person (and org) as their e-mail address.

- Provider key: '<conversation>:<message id>', e.g. 'chat:19:abc@thread.v2:1695..'
  or 'channel:<team id>:<channel id>:<message id>', unique across chats and channels.
- Thread: the chat ('chat:<id>'), or for a channel the reply chain
  ('channel:<team id>:<channel id>:<root message id>').
- Edits keep the newest version in the rows. Every version's JSON stays in the vault
  and is listed in message_original, so nothing a colleague wrote is ever lost.
- Deleted messages stay, marked deleted (location present = false, flag 'deleted');
  the text seen before the deletion is kept.
- System events (messageType other than 'message') are skipped.
- Shared files are SharePoint/OneDrive links: attachment rows with the link in attrs,
  no blob. Hosted inline images (hostedContents) are not fetched yet; their count is
  kept in the x-teams-hosted-images header.

Like Ingestor.ingest, the caller owns the transaction; each message runs in a savepoint,
and one that cannot be mapped is recorded in ingest_failure, never stopping a sync.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime

from psycopg.types.json import Jsonb

from talos import textclean
from talos.ingest import SEARCH_SQL, MAX_SEARCH_BODY, Ingestor, Location
from talos.mime import Address

log = logging.getLogger("talos.teams")

MAPPER_VERSION = 1  # stored as message.parser_version for Teams rows; bump when the mapping changes
MAX_CHAT_RECIPIENTS = 50  # a meeting chat can have hundreds of members; beyond this, no 'to' rows
FILE_TYPES = {"reference"}  # attachment contentType of a shared file
_HOSTED = re.compile(r"/hostedContents/", re.I)


def canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()


def dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass
class Conversation:
    """Where a message lives: one chat, or one reply chain in a channel."""

    kind: str                   # 'chat' or 'channel'
    key_prefix: str             # 'chat:<id>' or 'channel:<team>:<channel>'
    thread_key: str             # provider_thread_id
    folder: str                 # message_location.folder: 'Teams/Chats' or 'Teams/<team>/<channel>'
    subject: str | None = None  # chat topic or the root message's subject; None keeps the thread's
    fallback_subject: str | None = None
    member_ids: list[str] = field(default_factory=list)  # chat members, for 'to' participants
    meta: dict = field(default_factory=dict)             # chat_id, chat_type, team_id, channel_id …


class TeamsIngestor:
    def __init__(self, ingestor: Ingestor, account_id: str):
        self.ing = ingestor
        self.conn = ingestor.conn
        self.account_id = account_id
        self.my_ids: set[str] = set()
        self._users: dict[str, tuple[str | None, str | None]] = {}

    # ------------------------------------------------------------------ identity

    def set_me(self, user_id: str | None) -> None:
        if user_id:
            self.my_ids.add(user_id)

    def learn_members(self, members: list[dict]) -> None:
        """Remember user id → address from conversationMember records (they carry 'email')."""
        for m in members:
            uid = m.get("userId")
            if not uid:
                continue
            address = (m.get("email") or "").strip().lower() or None
            name = m.get("displayName") or None
            if self._users.get(uid) == (address, name):
                continue
            old = self._lookup(uid)
            address, name = address or old[0], name or old[1]
            self._users[uid] = (address, name)
            if old != (address, name):
                self.conn.execute(
                    "insert into teams_user (user_id, address, display_name) values (%s, %s, %s)"
                    " on conflict (user_id) do update set address = coalesce(excluded.address, teams_user.address),"
                    " display_name = coalesce(excluded.display_name, teams_user.display_name), updated_at = now()",
                    (uid, address, name))

    def _lookup(self, uid: str) -> tuple[str | None, str | None]:
        if uid in self._users:
            return self._users[uid]
        row = self.conn.execute("select address, display_name from teams_user where user_id = %s", (uid,)).fetchone()
        found = (row["address"], row["display_name"]) if row else (None, None)
        self._users[uid] = found
        return found

    def reset_caches(self) -> None:
        """After a rollback: teams_user rows written in it are gone, so forget what was cached."""
        self._users.clear()
        self.ing.reset_caches()

    # ------------------------------------------------------------------ public

    def ingest(self, conv: Conversation, msg: dict) -> str:
        """Returns 'added', 'updated', 'unchanged', 'skipped' (system event) or 'failed'."""
        if msg.get("messageType", "message") != "message":
            return "skipped"
        data = canonical(msg)
        sha = hashlib.sha256(data).hexdigest()
        key = f"{conv.key_prefix}:{msg['id']}"
        row = self.conn.execute(
            "select id, raw_sha256 from message where account_id = %s and provider_key = %s",
            (self.account_id, key)).fetchone()
        if row and row["raw_sha256"] == sha:
            self._observe(row["id"], conv, msg)
            return "unchanged"
        blob = self.ing.vault.put(data, "json")
        self.ing._blob_row(blob)
        try:
            with self.conn.transaction():  # a savepoint inside the caller's page
                if row:
                    self._update(row["id"], conv, msg, sha)
                    mid = row["id"]
                else:
                    mid = self._insert(conv, msg, sha, key)
                self.conn.execute("insert into message_original (message_id, sha256) values (%s, %s)"
                                  " on conflict do nothing", (mid, sha))
                self._observe(mid, conv, msg)
        except Exception as exc:
            self.reset_caches()
            self._failure(key, sha, conv, exc)
            return "failed"
        self.conn.execute("delete from ingest_failure where account_id = %s and provider_key = %s",
                          (self.account_id, key))
        return "updated" if row else "added"

    # ------------------------------------------------------------------ mapping

    def _sender(self, msg: dict) -> tuple[str | None, str | None, str | None, bool]:
        """(user id, address, name, automated) of whoever wrote the message."""
        frm = msg.get("from") or {}
        user = frm.get("user")
        if user and user.get("id"):
            address, name = self._lookup(user["id"])
            return user["id"], address, user.get("displayName") or name, False
        other = frm.get("application") or frm.get("device") or {}
        return None, None, other.get("displayName"), True

    def _text(self, msg: dict) -> tuple[str, str]:
        body = msg.get("body") or {}
        content = body.get("content") or ""
        if (body.get("contentType") or "html").lower() == "html":
            kind, text = "html", textclean.html_to_text(content)
        else:
            kind, text = "plain", textclean.tidy(content)
        cards = [_card_text(a.get("content")) for a in (msg.get("attachments") or [])
                 if str(a.get("contentType", "")).startswith("application/vnd.microsoft.card")]
        cards = [c for c in cards if c]
        if cards:
            text = "\n\n".join([text, *cards]).strip()
        return ("none" if not text else kind), text

    def _headers(self, conv: Conversation, msg: dict, uid: str | None) -> dict:
        body = (msg.get("body") or {}).get("content") or ""
        h = {
            "x-teams-kind": conv.kind,
            "x-teams-message-type": msg.get("messageType"),
            "x-teams-from-user": uid,
            "x-teams-reply-to": msg.get("replyToId"),
            "x-teams-importance": msg.get("importance"),
            "x-teams-web-url": msg.get("webUrl"),
            "x-teams-edited": msg.get("lastEditedDateTime"),
            "x-teams-deleted": msg.get("deletedDateTime"),
            "x-teams-hosted-images": str(len(_HOSTED.findall(body))) if _HOSTED.search(body) else None,
            "mapper_version": MAPPER_VERSION,
        }
        h.update({f"x-teams-{k.replace('_', '-')}": v for k, v in conv.meta.items()})
        return {k: v for k, v in h.items() if v is not None}

    def _files(self, msg: dict) -> list[dict]:
        return [a for a in (msg.get("attachments") or []) if a.get("contentType") in FILE_TYPES and a.get("contentUrl")]

    def _thread(self, conv: Conversation, at: datetime | None) -> tuple[int, str | None]:
        row = self.conn.execute(
            "select id, subject from thread where account_id = %s and provider_thread_id = %s",
            (self.account_id, conv.thread_key)).fetchone()
        if row:
            if conv.subject and conv.subject != row["subject"]:  # a chat renamed keeps its newest topic
                self.conn.execute("update thread set subject = %s where id = %s", (conv.subject, row["id"]))
                return row["id"], conv.subject
            return row["id"], row["subject"]
        tid = self.ing._entity("thread")
        subject = conv.subject or conv.fallback_subject
        self.conn.execute(
            "insert into thread (id, account_id, provider_thread_id, subject) values (%s, %s, %s, %s)",
            (tid, self.account_id, conv.thread_key, subject))
        return tid, subject

    def _insert(self, conv: Conversation, msg: dict, sha: str, key: str) -> int:
        created = dt(msg.get("createdDateTime"))
        thread_id, thread_subject = self._thread(conv, created)
        uid, address, name, automated = self._sender(msg)
        subject = msg.get("subject") or thread_subject or conv.fallback_subject
        body_kind, text = self._text(msg)
        files = self._files(msg)
        is_me = (uid in self.my_ids) or (address in self.ing.me if address else False)
        mid = self.ing._entity("message")
        self.conn.execute(
            """
            insert into message (id, account_id, medium, provider_key, thread_id, raw_sha256, sent_at, received_at,
                direction, from_address, from_name, subject, snippet, size_bytes, has_attachments, is_automated,
                in_reply_to, headers, parser_version)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (mid, self.account_id, "teams_chat" if conv.kind == "chat" else "teams_channel", key, thread_id, sha,
             created, created, "out" if is_me else "in", address, name, subject, textclean.snippet(text),
             len(canonical(msg)), bool(files), automated, msg.get("replyToId"),
             Jsonb(self._headers(conv, msg, uid)), MAPPER_VERSION))
        self.ing._edge(mid, "in_thread", thread_id)

        people_text = [f"{name or ''} {address or ''}".strip()]
        if address:
            self._participant(mid, "from", Address(address, name), 0)
        if conv.kind == "chat" and len(conv.member_ids) <= MAX_CHAT_RECIPIENTS:
            n = 0
            for member in conv.member_ids:
                if member == uid:
                    continue
                m_address, m_name = self._lookup(member)
                if m_address and m_address != address:
                    self._participant(mid, "to", Address(m_address, m_name), n)
                    people_text.append(f"{m_name or ''} {m_address}")
                    n += 1

        for f in files:
            self._file(mid, f)
        self.conn.execute(
            f"insert into message_text (message_id, body_kind, body_text, quote_stripped, search)"
            f" values (%(id)s, %(kind)s, %(text)s, %(text)s, {SEARCH_SQL})",
            {"id": mid, "kind": body_kind, "text": text, "subject": subject, "people": " ".join(people_text),
             "filenames": " ".join(f.get("name") or "" for f in files), "body": text[:MAX_SEARCH_BODY]})
        self.conn.execute(
            """
            update thread set
                message_count = message_count + 1,
                first_at = least(coalesce(first_at, %(t)s), %(t)s),
                last_at = greatest(coalesce(last_at, %(t)s), %(t)s)
            where id = %(id)s
            """, {"t": created, "id": thread_id})
        return mid

    def _update(self, mid: int, conv: Conversation, msg: dict, sha: str) -> None:
        """A newer version of a message already stored: an edit, a deletion, a reaction."""
        uid, address, name, automated = self._sender(msg)
        body_kind, text = self._text(msg)
        files = self._files(msg)
        keep_text = bool(msg.get("deletedDateTime")) and not text  # deleted: keep what was said before
        _, thread_subject = self._thread(conv, None)
        subject = msg.get("subject") or thread_subject or conv.fallback_subject
        self.conn.execute(
            "update message set raw_sha256 = %s, subject = %s, headers = %s, has_attachments = has_attachments or %s,"
            " from_address = coalesce(from_address, %s), from_name = coalesce(%s, from_name),"
            " snippet = case when %s then snippet else %s end where id = %s",
            (sha, subject, Jsonb(self._headers(conv, msg, uid)), bool(files), address, name, keep_text,
             textclean.snippet(text), mid))
        for f in files:
            self._file(mid, f)
        if not keep_text:
            people = " ".join(f"{r['name'] or ''} {r['address']}" for r in self.conn.execute(
                "select name, address from participant where message_id = %s", (mid,)))
            filenames = " ".join(r["filename"] or "" for r in self.conn.execute(
                "select filename from attachment where message_id = %s", (mid,)))
            self.conn.execute(
                f"update message_text set body_kind = %(kind)s, body_text = %(text)s, quote_stripped = %(text)s,"
                f" search = {SEARCH_SQL} where message_id = %(id)s",
                {"id": mid, "kind": body_kind, "text": text, "subject": subject, "people": people or name or "",
                 "filenames": filenames, "body": text[:MAX_SEARCH_BODY]})

    def _participant(self, mid: int, role: str, a: Address, ordinal: int) -> None:
        self.conn.execute(
            "insert into participant (message_id, role, address, name, ordinal) values (%s, %s, %s, %s, %s)"
            " on conflict do nothing", (mid, role, a.address, a.name, ordinal))
        self.ing._edge(mid, role, self.ing._person(a))

    def _file(self, mid: int, f: dict) -> None:
        """A shared file: a link to SharePoint or OneDrive, recorded without downloading it."""
        part = f"teams:{f.get('id') or f['contentUrl']}"
        if self.conn.execute("select 1 from attachment where message_id = %s and part_path = %s",
                             (mid, part)).fetchone():
            return
        name = f.get("name")
        ctype = (mimetypes.guess_type(name)[0] if name else None) or "application/octet-stream"
        aid = self.ing._entity("attachment")
        self.conn.execute(
            """
            insert into attachment (id, message_id, blob_sha256, part_path, filename, content_type, size_bytes,
                disposition, extract_status, attrs)
            values (%s, %s, null, %s, %s, %s, null, 'link', 'unsupported', %s)
            """,
            (aid, mid, part, name, ctype, Jsonb({"url": f["contentUrl"], "reference": True, "source": "teams",
                                                 "teams_content_type": f.get("contentType")})))
        self.ing._edge(mid, "has_attachment", aid)

    def _observe(self, mid: int, conv: Conversation, msg: dict) -> None:
        deleted = bool(msg.get("deletedDateTime"))
        self.ing.observe(mid, self.account_id, Location(
            folder=conv.folder, provider_key=f"{conv.key_prefix}:{msg['id']}", provider_id=msg["id"],
            flags=["seen"] + (["deleted"] if deleted else []), provider_thread_id=conv.thread_key))
        if deleted:
            self.conn.execute("update message_location set present = false where message_id = %s and folder = %s",
                              (mid, conv.folder))

    def _failure(self, key: str, sha: str, conv: Conversation, exc: Exception) -> None:
        log.warning("teams ingest failed account=%s key=%s error=%s", self.account_id, key, exc)
        self.conn.execute(
            "insert into ingest_failure (account_id, provider_key, raw_sha256, location, error)"
            " values (%s, %s, %s, %s, %s) on conflict (account_id, provider_key) do update set"
            " raw_sha256 = excluded.raw_sha256, location = excluded.location,"
            " error = excluded.error, attempts = ingest_failure.attempts + 1, last_at = now()",
            (self.account_id, key, sha, Jsonb({"kind": "teams", "conversation": asdict(conv)}),
             f"{type(exc).__name__}: {exc}"[:2000]))


def _card_text(content) -> str:
    """The readable strings of an Adaptive or hero card (bots, connectors, forms)."""
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return ""
    out: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("text", "title", "subtitle", "value") and isinstance(v, str) and v.strip():
                    out.append(v.strip())
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(content)
    return "\n".join(out)


def retry_failures(conn, vault, *, account_id: str | None = None) -> dict:
    """Re-map Teams messages that failed before, from their JSON originals in the vault."""
    rows = conn.execute(
        "select * from ingest_failure where location->>'kind' = 'teams'"
        + (" and account_id = %s" if account_id else ""), (account_id,) if account_id else ()).fetchall()
    fixed = still = 0
    ing = Ingestor(conn, vault)
    for r in rows:
        tin = TeamsIngestor(ing, r["account_id"])
        me = conn.execute("select state from sync_cursor where account_id = %s and scope = 'me'",
                          (r["account_id"],)).fetchone()
        tin.set_me((me["state"] if me else {}).get("id"))
        conv = Conversation(**r["location"]["conversation"])
        msg = json.loads(vault.get(r["raw_sha256"], "json"))
        with conn.transaction():
            res = tin.ingest(conv, msg)
        fixed += res in ("added", "updated", "unchanged")
        still += res == "failed"
    return {"fixed": fixed, "still_failing": still}
