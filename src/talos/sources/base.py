"""What every adapter shares: cursors, run bookkeeping, flag normalisation, and the IMAP fetch helpers
(size_batches, fetch_complete, catch_up, prune_fetch_failures, raise_imap_line_limit) that the Gmail and
plain IMAP adapters both use."""

from __future__ import annotations

import imaplib
import logging
from dataclasses import dataclass, field
from typing import Protocol

import psycopg
from psycopg.types.json import Jsonb

from talos.ingest import Ingestor

log = logging.getLogger("talos.sync")

# imaplib refuses any response line over imaplib._MAXLINE (1,000,000 bytes). A SEARCH
# answer is a single line, so a folder with more than about 135k UIDs cannot even be
# listed. Bodies are literals, read by length, and are not affected by this limit.
IMAP_MAX_LINE = 64 * 1024 * 1024
# How much message data one FETCH may bring back. A message larger than this is fetched alone.
MAX_BATCH_BYTES = 50 * 1024 * 1024
SIZE_CHUNK = 2000  # UIDs per RFC822.SIZE fetch


def raise_imap_line_limit(limit: int = IMAP_MAX_LINE) -> None:
    if getattr(imaplib, "_MAXLINE", 0) < limit:
        imaplib._MAXLINE = limit


raise_imap_line_limit()

IMAP_FLAGS = {
    "\\seen": "seen",
    "\\flagged": "flagged",
    "\\answered": "answered",
    "\\draft": "draft",
    "\\deleted": "deleted",
}


def normalize_flags(flags) -> list[str]:
    out = []
    for f in flags or ():
        s = f.decode("ascii", "replace") if isinstance(f, bytes) else str(f)
        mapped = IMAP_FLAGS.get(s.lower())
        if mapped:
            out.append(mapped)
        elif not s.startswith("\\"):
            out.append("kw:" + s)  # a server keyword, kept so rules can see it
    return sorted(set(out))


@dataclass
class SyncStats:
    seen: int = 0
    added: int = 0
    updated: int = 0
    gone: int = 0
    failed: int = 0
    notes: list[str] = field(default_factory=list)


class Cursors:
    def __init__(self, conn: psycopg.Connection, account_id: str):
        self.conn = conn
        self.account_id = account_id

    def get(self, scope: str) -> dict | None:
        row = self.conn.execute(
            "select state from sync_cursor where account_id = %s and scope = %s", (self.account_id, scope)).fetchone()
        return row["state"] if row else None

    def put(self, scope: str, state: dict) -> None:
        """Call inside the same transaction as the messages the cursor covers."""
        self.conn.execute(
            "insert into sync_cursor (account_id, scope, state, updated_at) values (%s, %s, %s, now())"
            " on conflict (account_id, scope) do update set state = excluded.state, updated_at = now()",
            (self.account_id, scope, Jsonb(state)))

    def all(self) -> dict[str, dict]:
        rows = self.conn.execute(
            "select scope, state from sync_cursor where account_id = %s", (self.account_id,)).fetchall()
        return {r["scope"]: r["state"] for r in rows}


@dataclass
class SyncContext:
    conn: psycopg.Connection
    ingestor: Ingestor
    cursors: Cursors
    account_id: str
    limit: int | None = None  # stop after this many new messages (backfill in slices)


class Source(Protocol):
    def sync(self, ctx: SyncContext) -> SyncStats: ...


def run(conn: psycopg.Connection, ingestor: Ingestor, account_id: str, source: Source,
        *, limit: int | None = None, quiet: bool = False) -> SyncStats:
    """Run one sync for one account and record it in sync_run.

    quiet (the Teams fast lane, every 20 seconds): a run that saw nothing and failed nowhere leaves no
    sync_run row, so the history holds what happened rather than thousands of empty checks."""
    if quiet:
        return _quiet_run(conn, ingestor, account_id, source, limit)
    with conn.transaction():
        run_id = conn.execute(
            "insert into sync_run (account_id) values (%s) returning id", (account_id,)).fetchone()["id"]
    ctx = SyncContext(conn, ingestor, Cursors(conn, account_id), account_id, limit)
    try:
        stats = source.sync(ctx)
    except Exception as exc:
        ingestor.reset_caches()
        with conn.transaction():
            conn.execute(
                "update sync_run set finished_at = now(), status = 'failed', error = %s where id = %s",
                (f"{type(exc).__name__}: {exc}"[:2000], run_id))
        log.exception("sync failed account=%s", account_id)
        raise
    with conn.transaction():
        conn.execute(
            "update sync_run set finished_at = now(), status = %s, seen = %s, added = %s, updated = %s,"
            " error = %s where id = %s",
            ("partial" if stats.notes or stats.failed else "ok", stats.seen, stats.added, stats.updated,
             "; ".join(stats.notes + ([f"{stats.failed} messages failed to fetch or ingest;"
                                       " see fetch_failure and ingest_failure"]
                                      if stats.failed else []))[:2000] or None, run_id))
    log.info("sync done account=%s seen=%d added=%d updated=%d gone=%d failed=%d",
             account_id, stats.seen, stats.added, stats.updated, stats.gone, stats.failed)
    return stats


def _quiet_run(conn: psycopg.Connection, ingestor: Ingestor, account_id: str, source: Source,
               limit: int | None) -> SyncStats:
    ctx = SyncContext(conn, ingestor, Cursors(conn, account_id), account_id, limit)
    started = conn.execute("select now() as t").fetchone()["t"]
    try:
        stats = source.sync(ctx)
    except Exception as exc:
        ingestor.reset_caches()
        with conn.transaction():
            conn.execute("insert into sync_run (account_id, started_at, finished_at, status, error)"
                         " values (%s, %s, now(), 'failed', %s)", (account_id, started, f"{type(exc).__name__}: {exc}"[:2000]))
        log.exception("sync failed account=%s", account_id)
        raise
    if stats.seen or stats.failed or stats.notes:
        with conn.transaction():
            conn.execute(
                "insert into sync_run (account_id, started_at, finished_at, status, seen, added, updated, error)"
                " values (%s, %s, now(), %s, %s, %s, %s, %s)",
                (account_id, started, "partial" if stats.notes or stats.failed else "ok", stats.seen, stats.added,
                 stats.updated, "; ".join(stats.notes)[:2000] or None))
    return stats


def chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ---------------------------------------------------------------- shared by the IMAP adapters

def item(d: dict, name: str):
    """A FETCH item by name; IMAPClient keys them as bytes."""
    key = name.encode()
    return d[key] if key in d else d.get(name)


def uid_ref(folder: str, uidvalidity: int, uid: int) -> str:
    """How fetch_failure names a UID whose body never arrived."""
    return f"{folder}:{uidvalidity}:{uid}"


def new_uids(client, last_uid: int) -> list[int]:
    """UIDs above last_uid. 'N:*' always matches the highest UID, even when it is below N."""
    return sorted(u for u in client.search(["UID", f"{last_uid + 1}:*"]) if u > last_uid)


def size_batches(client, uids: list[int], *, max_count: int, max_bytes: int | None = None):
    """Split UIDs into FETCH batches of at most max_count messages and max_bytes of mail,
    going by RFC822.SIZE, so a run of large messages never lands in memory at once."""
    max_bytes = MAX_BATCH_BYTES if max_bytes is None else max_bytes
    for part in chunks(uids, SIZE_CHUNK):
        wanted = set(part)
        sizes = {}
        for uid, d in client.fetch(part, ["RFC822.SIZE"]).items():
            size = item(d, "RFC822.SIZE")
            if uid in wanted and isinstance(size, int):
                sizes[uid] = size
        batch: list[int] = []
        total = 0
        for uid in part:
            size = sizes.get(uid, 0)
            if batch and (len(batch) >= max_count or total + size > max_bytes):
                yield batch
                batch, total = [], 0
            batch.append(uid)
            total += size
        if batch:
            yield batch


def fetch_complete(client, uids: list[int], items: list[str], complete) -> tuple[dict[int, dict], list[int]]:
    """FETCH uids; an entry the server left out, or one without the items asked for, is fetched
    again on its own. Returns (entries, still missing). An entry without the asked-for items is
    never used as data: IMAPClient keys an unsolicited FETCH (flags only, no UID) by sequence
    number, which can look like one of our UIDs."""
    data = client.fetch(uids, items)
    got = {u: data[u] for u in uids if u in data and complete(data[u])}
    for u in uids:
        if u in got:
            continue
        d = client.fetch([u], items).get(u)
        if d and complete(d):
            got[u] = d
    return got, [u for u in uids if u not in got]


def catch_up(conn: psycopg.Connection, account_id: str, folder: str, uidvalidity: int, server_uids: list[int],
             last_uid: int, *, skip=()) -> list[int]:
    """UIDs at or below the cursor that the server has, but that Talos neither holds nor lists
    as failed to ingest: skipped by an older sync, or left out of a FETCH. Fetched again every
    run until they arrive."""
    held = {r["uid"] for r in conn.execute(
        "select uid from message_location where account_id = %s and folder = %s and uidvalidity = %s"
        " and uid is not null", (account_id, folder, uidvalidity))}
    failed = {int(r["uid"]) for r in conn.execute(
        "select location ->> 'uid' as uid from ingest_failure where account_id = %s"
        " and location ->> 'folder' = %s and location ->> 'uidvalidity' = %s and location ->> 'uid' is not null",
        (account_id, folder, str(uidvalidity)))}
    skip = set(skip)
    return [u for u in server_uids if u <= last_uid and u not in held and u not in failed and u not in skip]


def prune_fetch_failures(conn: psycopg.Connection, account_id: str, folder: str, uidvalidity: int,
                         server_uids: list[int]) -> None:
    """Forget fetch failures for UIDs the folder no longer has (or under an older UIDVALIDITY)."""
    on_server = set(server_uids)
    stale = [r["ref"] for r in conn.execute(
        "select ref, location from fetch_failure where account_id = %s and location ->> 'folder' = %s",
        (account_id, folder))
        if r["location"].get("uidvalidity") != uidvalidity or r["location"].get("uid") not in on_server]
    if stale:
        conn.execute("delete from fetch_failure where account_id = %s and ref = any(%s)", (account_id, stale))
