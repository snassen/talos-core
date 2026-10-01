"""L0 → L1: one raw message in, vault blob + rows + graph edges out.

Ingest is idempotent per (account, provider_key). Seeing a message again only
refreshes its server-state mirror (folder, flags, labels). The caller owns the
transaction, so a sync adapter can commit a batch of messages together with the
cursor that covers them: a crash then never skips or duplicates mail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from talos import extract as extractor
from talos import mime
from talos.mime import Address, ParsedMessage
from talos.vault import BlobRef, BlobUnavailable, Vault

log = logging.getLogger("talos.ingest")

# Mail providers, not organisations: a gmail.com sender says nothing about who they work for.
FREEMAIL = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.se", "outlook.com", "outlook.se",
    "live.com", "live.se", "msn.com", "icloud.com", "me.com", "mac.com", "yahoo.com", "yahoo.se",
    "aol.com", "proton.me", "protonmail.com", "gmx.com", "gmx.net", "telia.com", "spray.se",
    "bredband.net", "comhem.se", "passagen.se",
}

SEARCH_SQL = """
    setweight(to_tsvector('swedish', left(coalesce(%(subject)s, ''), 2000)), 'A')
 || setweight(to_tsvector('english', left(coalesce(%(subject)s, ''), 2000)), 'A')
 || setweight(to_tsvector('simple',  left(coalesce(%(people)s, ''), 2000)), 'B')
 || setweight(to_tsvector('simple',  left(coalesce(%(filenames)s, ''), 2000)), 'B')
 || setweight(to_tsvector('swedish', coalesce(%(body)s, '')), 'C')
 || setweight(to_tsvector('english', coalesce(%(body)s, '')), 'C')
"""
MAX_SEARCH_BODY = 100_000


@dataclass
class Location:
    """Where a provider says the message is, and what it says about it."""

    folder: str
    provider_key: str
    uidvalidity: int | None = None
    uid: int | None = None
    provider_id: str | None = None
    flags: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    provider_thread_id: str | None = None
    received_at: datetime | None = None
    folder_path: str | None = None  # readable path when folder is an id (Graph); defaults to folder

    def as_json(self) -> dict:
        return {"folder": self.folder, "folder_path": self.folder_path, "provider_key": self.provider_key,
                "uidvalidity": self.uidvalidity, "uid": self.uid, "provider_id": self.provider_id,
                "flags": self.flags, "labels": self.labels, "provider_thread_id": self.provider_thread_id,
                "received_at": self.received_at.isoformat() if self.received_at else None}

    @classmethod
    def from_json(cls, where: dict) -> "Location":
        return cls(folder=where["folder"], provider_key=where["provider_key"], uidvalidity=where.get("uidvalidity"),
                   uid=where.get("uid"), provider_id=where.get("provider_id"), flags=where.get("flags") or [],
                   labels=where.get("labels") or [], provider_thread_id=where.get("provider_thread_id"),
                   received_at=datetime.fromisoformat(where["received_at"]) if where.get("received_at") else None,
                   folder_path=where.get("folder_path"))


@dataclass
class IngestResult:
    message_id: int | None
    created: bool
    failed: bool = False


class Ingestor:
    def __init__(self, conn: psycopg.Connection, vault: Vault, *, extract_inline: bool = True):
        self.conn = conn
        self.vault = vault
        self.extract_inline = extract_inline
        self._people: dict[str, int] = {}
        self._orgs: dict[str, int] = {}
        self.reload_me()

    def reload_me(self) -> None:
        self.me = {r["address"].lower() for r in self.conn.execute("select address from my_address")}

    def reset_caches(self) -> None:
        """Call after a rolled-back transaction: cached person/org ids may no longer exist."""
        self._people.clear()
        self._orgs.clear()

    # ------------------------------------------------------------------ public

    def ingest(self, account_id: str, raw: bytes, loc: Location) -> IngestResult:
        row = self.conn.execute(
            "select id from message where account_id = %s and provider_key = %s",
            (account_id, loc.provider_key),
        ).fetchone()
        if row:
            self.observe(row["id"], account_id, loc)
            return IngestResult(row["id"], False)

        # The original goes to the vault and the blob table first, in a savepoint of its own, so a
        # parse failure below can still name it in ingest_failure. Input that is not a message
        # at all (no body: None, or not bytes) has no original to keep; it is a fetch failure,
        # and the caller's batch carries on either way.
        try:
            if not isinstance(raw, (bytes, bytearray)):
                raise TypeError(f"expected the raw message as bytes, got {type(raw).__name__}")
            raw = bytes(raw)
            blob = self.vault.put(raw, "raw")
            with self.conn.transaction():
                self._blob_row(blob)
        except Exception as exc:
            self.record_fetch_failure(account_id, loc.provider_key, loc, exc)
            return IngestResult(None, False, failed=True)
        try:
            with self.conn.transaction():  # a savepoint inside the caller's batch
                parsed = mime.parse(raw)
                message_id = self._message(account_id, blob, parsed, loc)
                self.observe(message_id, account_id, loc)
        except Exception as exc:
            self.reset_caches()
            self._failure(account_id, blob.sha256, loc, exc)
            return IngestResult(None, False, failed=True)
        self.conn.execute("delete from ingest_failure where account_id = %s and provider_key = %s",
                          (account_id, loc.provider_key))
        self.conn.execute("delete from fetch_failure where account_id = %s and ref = %s",
                          (account_id, loc.provider_key))
        return IngestResult(message_id, True)

    def _failure(self, account_id: str, sha: str, loc: Location, exc: Exception) -> None:
        log.warning("ingest failed account=%s key=%s error=%s", account_id, loc.provider_key, exc)
        where = loc.as_json()
        self.conn.execute(
            "insert into ingest_failure (account_id, provider_key, raw_sha256, location, error)"
            " values (%s, %s, %s, %s, %s) on conflict (account_id, provider_key) do update set"
            " error = excluded.error, attempts = ingest_failure.attempts + 1, last_at = now()",
            (account_id, loc.provider_key, sha, Jsonb(where), f"{type(exc).__name__}: {exc}"[:2000]))

    def record_fetch_failure(self, account_id: str, ref: str, loc: Location | dict | None,
                             exc: Exception | str) -> None:
        """A message whose body never arrived. The adapter retries it on its next run."""
        error = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
        log.warning("fetch failed account=%s ref=%s error=%s", account_id, ref, error)
        where = loc.as_json() if isinstance(loc, Location) else (loc or {})
        self.conn.execute(
            "insert into fetch_failure (account_id, ref, location, error) values (%s, %s, %s, %s)"
            " on conflict (account_id, ref) do update set location = excluded.location, error = excluded.error,"
            " attempts = fetch_failure.attempts + 1, last_at = now()",
            (account_id, ref, Jsonb(where), error[:2000]))

    def clear_fetch_failures(self, account_id: str, refs) -> None:
        refs = list(refs)
        if refs:
            self.conn.execute("delete from fetch_failure where account_id = %s and ref = any(%s)", (account_id, refs))

    def fetch_failures(self, account_id: str) -> list[dict]:
        return self.conn.execute(
            "select ref, location, error, attempts from fetch_failure where account_id = %s order by first_at, ref",
            (account_id,)).fetchall()

    def observe(self, message_id: int, account_id: str, loc: Location) -> None:
        """Record the server's current view of a message (its location, flags, labels).

        observed_at is the wall clock, not the transaction's start, so a full re-read of a
        folder can tell what it saw from what it did not (see mark_folder_gone)."""
        self.conn.execute(
            """
            insert into message_location (message_id, account_id, folder, folder_path, uidvalidity, uid, provider_id,
                                          flags, labels, present, observed_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, true, clock_timestamp())
            on conflict (message_id, folder) do update set
                folder_path = excluded.folder_path,
                uidvalidity = excluded.uidvalidity, uid = excluded.uid, provider_id = excluded.provider_id,
                flags = excluded.flags, labels = excluded.labels, present = true, observed_at = clock_timestamp()
            """,
            (message_id, account_id, loc.folder, loc.folder_path or loc.folder, loc.uidvalidity, loc.uid,
             loc.provider_id, sorted(set(loc.flags)), sorted(set(loc.labels))),
        )

    def mark_gone(self, account_id: str, folder: str, *, provider_ids: list[str] | None = None,
                  uids: list[int] | None = None) -> int:
        """The server no longer has these. The originals stay in the vault."""
        if provider_ids:
            cur = self.conn.execute(
                "update message_location set present = false, observed_at = now()"
                " where account_id = %s and folder = %s and provider_id = any(%s) and present",
                (account_id, folder, provider_ids))
            return cur.rowcount
        if uids:
            cur = self.conn.execute(
                "update message_location set present = false, observed_at = now()"
                " where account_id = %s and folder = %s and uid = any(%s) and present",
                (account_id, folder, uids))
            return cur.rowcount
        return 0

    def mark_folder_gone(self, account_id: str, folder: str, *, observed_before=None) -> int:
        """Every location in a folder the server no longer has (or, with observed_before, every
        location a full re-read of the folder did not see again)."""
        cur = self.conn.execute(
            "update message_location set present = false, observed_at = now()"
            " where account_id = %s and folder = %s and present"
            + (" and observed_at < %s" if observed_before is not None else ""),
            (account_id, folder) + ((observed_before,) if observed_before is not None else ()))
        return cur.rowcount

    # ------------------------------------------------------------------ internals

    def _entity(self, kind: str) -> int:
        return self.conn.execute("insert into entity (kind) values (%s) returning id", (kind,)).fetchone()["id"]

    def _edge(self, src: int, rel: str, dst: int, source: str = "ingest") -> None:
        self.conn.execute(
            "insert into edge (src, rel, dst, source) values (%s, %s, %s, %s) on conflict do nothing",
            (src, rel, dst, source))

    def _blob_row(self, blob: BlobRef) -> None:
        self.conn.execute(
            "insert into blob (sha256, kind, size, stored_size, codec, path) values (%s, %s, %s, %s, %s, %s)"
            " on conflict (sha256) do nothing",
            (blob.sha256, blob.kind, blob.size, blob.stored_size, blob.codec, blob.path))

    def _direction(self, p: ParsedMessage) -> str:
        if not p.from_ or p.from_.address not in self.me:
            return "in"
        recipients = [a.address for a in (p.to + p.cc + p.bcc)]
        if recipients and all(r in self.me for r in recipients):
            return "self"
        return "out"

    def _org(self, domain: str) -> int | None:
        if not domain or domain in FREEMAIL:
            return None
        if domain in self._orgs:
            return self._orgs[domain]
        row = self.conn.execute("select id from org where domain = %s", (domain,)).fetchone()
        if row:
            oid = row["id"]
        else:
            # Another sync may be adding the same domain right now: insert-or-nothing, then read
            # whichever row won. A lost race leaves our entity row unused, so it goes again.
            oid = self._entity("org")
            won = self.conn.execute("insert into org (id, domain) values (%s, %s) on conflict (domain) do nothing"
                                    " returning id", (oid, domain)).fetchone()
            if not won:
                self.conn.execute("delete from entity where id = %s", (oid,))
                oid = self.conn.execute("select id from org where domain = %s", (domain,)).fetchone()["id"]
        self._orgs[domain] = oid
        return oid

    def _person(self, a: Address) -> int:
        if a.address in self._people:
            return self._people[a.address]
        row = self.conn.execute("select person_id from address where address = %s", (a.address,)).fetchone()
        if row:
            pid = row["person_id"]
            if a.name:
                self.conn.execute(
                    "update person set display_name = %s where id = %s and display_name is null", (a.name, pid))
        else:
            org_id = self._org(a.domain)
            pid = self._entity("person")
            won = self.conn.execute(
                "insert into person (id, display_name, primary_address, is_me, org_id) values (%s, %s, %s, %s, %s)"
                " on conflict (primary_address) do nothing returning id",
                (pid, a.name, a.address, a.address in self.me, org_id)).fetchone()
            if won:
                self.conn.execute("insert into address (address, person_id, domain) values (%s, %s, %s)"
                                  " on conflict (address) do nothing", (a.address, pid, a.domain))
                if org_id:
                    self._edge(pid, "works_at", org_id)
            else:  # another session added this person first; use theirs
                self.conn.execute("delete from entity where id = %s", (pid,))
            row = self.conn.execute("select person_id from address where address = %s", (a.address,)).fetchone() \
                or self.conn.execute("select id as person_id from person where primary_address = %s",
                                     (a.address,)).fetchone()
            pid = row["person_id"]
        self._people[a.address] = pid
        return pid

    def _thread(self, account_id: str, p: ParsedMessage, loc: Location) -> int:
        if loc.provider_thread_id:
            row = self.conn.execute(
                "select id from thread where account_id = %s and provider_thread_id = %s",
                (account_id, loc.provider_thread_id)).fetchone()
            if row:
                return row["id"]
        else:
            refs = [r for r in ([p.in_reply_to] + p.references) if r]
            if refs:
                row = self.conn.execute(
                    "select thread_id from message where account_id = %s and rfc_message_id = any(%s)"
                    " and thread_id is not null order by received_at limit 1",
                    (account_id, refs)).fetchone()
                if row:
                    return row["thread_id"]
        tid = self._entity("thread")
        self.conn.execute(
            "insert into thread (id, account_id, provider_thread_id, subject) values (%s, %s, %s, %s)",
            (tid, account_id, loc.provider_thread_id, p.subject))
        return tid

    def _message(self, account_id: str, blob: BlobRef, p: ParsedMessage, loc: Location) -> int:
        thread_id = self._thread(account_id, p, loc)
        mid = self._entity("message")
        received = loc.received_at or p.sent_at
        self.conn.execute(
            """
            insert into message (id, account_id, provider_key, rfc_message_id, thread_id, raw_sha256, sent_at,
                received_at, direction, from_address, from_name, subject, snippet, size_bytes, has_attachments,
                is_automated, list_id, in_reply_to, references_ids, headers, parser_version)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (mid, account_id, loc.provider_key, p.message_id, thread_id, blob.sha256, p.sent_at, received,
             self._direction(p), p.from_.address if p.from_ else None, p.from_.name if p.from_ else None,
             p.subject, p.snippet, p.size,
             any(a.is_attachment for a in p.attachments),
             p.is_automated, p.list_id, p.in_reply_to, p.references,
             Jsonb({**p.headers, "automated": p.automated_reasons, "defects": p.defects}),
             mime.PARSER_VERSION))
        # Its subject pattern (sender + skeleton, talos.enrich), so a new mail is grouped at once.
        self.conn.execute(
            "insert into message_pattern (message_id, pattern_key, skeleton)"
            " values (%s, subject_pattern_key(%s, %s), subject_skeleton(%s))",
            (mid, p.from_.address if p.from_ else None, p.subject, p.subject))
        self._edge(mid, "in_thread", thread_id)

        people_text = []
        for role, addrs in (("from", [p.from_] if p.from_ else []), ("sender", [p.sender] if p.sender else []),
                            ("reply_to", p.reply_to), ("to", p.to), ("cc", p.cc), ("bcc", p.bcc)):
            for i, a in enumerate(addrs):
                self.conn.execute(
                    "insert into participant (message_id, role, address, name, ordinal) values (%s, %s, %s, %s, %s)"
                    " on conflict do nothing", (mid, role, a.address, a.name, i))
                pid = self._person(a)
                if role in ("from", "to", "cc", "bcc"):
                    self._edge(mid, role, pid)
                people_text.append(f"{a.name or ''} {a.address}")

        filenames = []
        for part in p.attachments:
            filenames.append(part.filename or "")
            self._attachment(mid, part)

        self.conn.execute(
            f"insert into message_text (message_id, body_kind, body_text, quote_stripped, search)"
            f" values (%(id)s, %(kind)s, %(text)s, %(stripped)s, {SEARCH_SQL})",
            {"id": mid, "kind": p.body_kind, "text": p.body_text, "stripped": p.quote_stripped,
             "subject": p.subject, "people": " ".join(people_text), "filenames": " ".join(filenames),
             "body": (p.quote_stripped or p.body_text)[:MAX_SEARCH_BODY]})

        self.conn.execute(
            """
            update thread set
                message_count = message_count + 1,
                first_at = least(coalesce(first_at, %(t)s), %(t)s),
                last_at = greatest(coalesce(last_at, %(t)s), %(t)s),
                subject = coalesce(subject, %(s)s)
            where id = %(id)s
            """, {"t": received, "s": p.subject, "id": thread_id})
        return mid

    def _attachment(self, message_id: int, part: mime.Part) -> None:
        blob = self.vault.put(part.data, "attachment")
        self._blob_row(blob)
        aid = self._entity("attachment")
        status, text, attrs = "pending", None, {}
        if self.extract_inline:
            ex = extractor.extract(part.data, part.content_type, part.filename)
            status, text, attrs = ex.status, ex.text, ex.attrs
        self.conn.execute(
            """
            insert into attachment (id, message_id, blob_sha256, part_path, filename, content_type, size_bytes,
                disposition, content_id, extract_status, extracted_text, attrs)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (message_id, part_path) do nothing
            """,
            (aid, message_id, blob.sha256, part.part_path, part.filename, part.content_type, len(part.data),
             part.disposition, part.content_id, status, text, Jsonb(attrs)))
        self._edge(message_id, "has_attachment", aid)


def extract_pending(conn: psycopg.Connection, vault: Vault, *, limit: int = 500) -> int:
    """Run extraction for attachments ingested with extract_inline=False. Returns how many it did."""
    rows = conn.execute(
        "select id, blob_sha256, content_type, filename from attachment where extract_status = 'pending'"
        " order by id limit %s", (limit,)).fetchall()
    for r in rows:
        try:
            data = vault.get(r["blob_sha256"], "attachment")
        except BlobUnavailable as exc:
            conn.execute(
                "update attachment set extract_status = 'error', attrs = attrs || %s where id = %s",
                (Jsonb({"error": str(exc), "unavailable": True}), r["id"]))
            continue
        ex = extractor.extract(data, r["content_type"], r["filename"])
        conn.execute(
            "update attachment set extract_status = %s, extracted_text = %s, attrs = %s where id = %s",
            (ex.status, ex.text, Jsonb(ex.attrs), r["id"]))
    return len(rows)


def retry_failures(conn: psycopg.Connection, vault: Vault, *, account_id: str | None = None) -> dict:
    """Re-ingest messages that failed before, from their originals in the vault."""
    rows = conn.execute("select * from ingest_failure where location->>'kind' is distinct from 'teams'"
                        + (" and account_id = %s" if account_id else ""), (account_id,) if account_id else ()).fetchall()
    ing = Ingestor(conn, vault)
    fixed = still = 0
    for r in rows:
        loc = Location.from_json(r["location"])
        with conn.transaction():
            res = ing.ingest(r["account_id"], vault.get(r["raw_sha256"], "raw"), loc)
        fixed += res.created
        still += res.failed
    from talos import teams_ingest  # Teams originals are JSON, re-mapped by their own code
    teams = teams_ingest.retry_failures(conn, vault, account_id=account_id)
    return {"fixed": fixed + teams["fixed"], "still_failing": still + teams["still_failing"]}
