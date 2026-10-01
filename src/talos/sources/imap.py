"""Plain IMAP (iCloud, Loopia), folder by folder, read-only.

Without Gmail's permanent ids the provider key is the SHA-256 of the raw message, so
a message filed in two folders becomes one message with two locations. The cursor is
per folder: uidvalidity and last_uid. Flag changes on older mail are refreshed with
CONDSTORE when the server has it, and otherwise with a plain FLAGS fetch of the
folder, which is cheap at these accounts' sizes.

As for Gmail: new mail is UID SEARCH last_uid+1:*, bodies come in batches bounded by
RFC822.SIZE, a UID left out of a FETCH is asked for again and otherwise listed in
fetch_failure, and every run catches up on UIDs below the cursor that Talos does not
hold. A UID whose content duplicates another in the same folder resolves to a message
Talos already has at another UID; the cursor lists it under "aliases" so the catch-up
does not fetch it again every run.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Callable

from talos.ingest import Location
from talos.sources.base import (SyncContext, SyncStats, catch_up, chunks, fetch_complete, item, new_uids,
                                normalize_flags, prune_fetch_failures, raise_imap_line_limit, size_batches, uid_ref)

log = logging.getLogger("talos.sync.imap")

BATCH = 50
SKIP_FLAGS = {b"\\Noselect", b"\\NonExistent"}
FETCH = ["FLAGS", "INTERNALDATE", "BODY.PEEK[]"]
MAX_BATCH_BYTES = None  # None: base.MAX_BATCH_BYTES

raise_imap_line_limit()


def _complete(d: dict) -> bool:
    return isinstance(item(d, "BODY[]"), (bytes, bytearray))


class ImapSource:
    def __init__(self, address: str, password: Callable[[], str], host: str, *, port: int = 993,
                 username: str | None = None, client_factory: Callable | None = None):
        self.address = address
        self.username = username or address
        self.password = password
        self.host = host
        self.port = port
        self.client_factory = client_factory

    def _connect(self):
        if self.client_factory:
            return self.client_factory()
        from imapclient import IMAPClient
        client = IMAPClient(self.host, port=self.port, ssl=True, timeout=120)
        client.normalise_times = False
        client.login(self.username, self.password())
        return client

    def sync(self, ctx: SyncContext) -> SyncStats:
        stats = SyncStats()
        client = self._connect()
        remaining = ctx.limit
        try:
            for flags, _delim, name in client.list_folders():
                if SKIP_FLAGS & set(flags):
                    continue
                if remaining is not None and remaining <= 0:
                    stats.notes.append("limit reached; later folders not visited")
                    break
                added = self._folder(ctx, client, name, stats, remaining)
                if remaining is not None:
                    remaining -= added
        finally:
            try:
                client.logout()
            except Exception:
                pass
        return stats

    def _folder(self, ctx: SyncContext, client, folder: str, stats: SyncStats, limit: int | None) -> int:
        info = client.select_folder(folder, readonly=True)
        uidvalidity = int(info.get(b"UIDVALIDITY", info.get("UIDVALIDITY", 0)))
        scope = f"folder:{folder}"
        cur = ctx.cursors.get(scope) or {}
        if cur.get("uidvalidity") != uidvalidity:
            cur = {"uidvalidity": uidvalidity, "last_uid": 0}
        server_uids = sorted(client.search(["ALL"]))
        on_server = set(server_uids)
        stats.seen += len(server_uids)
        last = cur["last_uid"]
        aliases = {u for u in cur.get("aliases", []) if u in on_server}
        missed = catch_up(ctx.conn, ctx.account_id, folder, uidvalidity, server_uids, last, skip=aliases)
        new = sorted(set(missed) | set(new_uids(client, last)))
        if limit is not None:
            new = new[:limit]
        failed_refs = {r["ref"] for r in ctx.ingestor.fetch_failures(ctx.account_id)}
        added = 0
        for chunk in size_batches(client, new, max_count=BATCH, max_bytes=MAX_BATCH_BYTES):
            data, missing = fetch_complete(client, chunk, FETCH, _complete)
            try:
                with ctx.conn.transaction():
                    for uid in chunk:
                        ref = uid_ref(folder, uidvalidity, uid)
                        if uid in missing:
                            ctx.ingestor.record_fetch_failure(
                                ctx.account_id, ref, {"folder": folder, "uidvalidity": uidvalidity, "uid": uid},
                                "the server returned no body for this UID")
                            stats.failed += 1
                            continue
                        d = data[uid]
                        raw = bytes(item(d, "BODY[]"))
                        loc = Location(folder=folder, folder_path=folder,
                                       provider_key="sha256:" + hashlib.sha256(raw).hexdigest(),
                                       uidvalidity=uidvalidity, uid=uid, flags=normalize_flags(item(d, "FLAGS")),
                                       received_at=item(d, "INTERNALDATE"))
                        if uid <= last and self._held_elsewhere(ctx, folder, loc):
                            aliases.add(uid)  # same content as a message this folder holds at another UID
                            continue
                        res = ctx.ingestor.ingest(ctx.account_id, raw, loc)
                        stats.added += res.created
                        stats.failed += res.failed
                        added += res.created
                        if ref in failed_refs:
                            ctx.ingestor.clear_fetch_failures(ctx.account_id, [ref])
                    last = max(last, max(chunk))
                    cur = {**cur, "last_uid": last, "aliases": sorted(aliases)}
                    ctx.cursors.put(scope, cur)
            except Exception:
                ctx.ingestor.reset_caches()
                raise
        # Refresh flags on known mail and notice what left the folder.
        known = {r["uid"]: r["message_id"] for r in ctx.conn.execute(
            "select uid, message_id from message_location where account_id = %s and folder = %s"
            " and uidvalidity = %s and present", (ctx.account_id, folder, uidvalidity))}
        with ctx.conn.transaction():
            old = [u for u in server_uids if u in known]
            for chunk in chunks(old, 500):
                asked = set(chunk)
                for uid, d in client.fetch(chunk, ["FLAGS"]).items():
                    flags = item(d, "FLAGS")
                    if uid not in asked or uid not in known or flags is None:
                        continue  # not an answer to this FETCH (an unsolicited one, keyed by sequence number)
                    ctx.conn.execute(
                        "update message_location set flags = %s, observed_at = now()"
                        " where message_id = %s and folder = %s",
                        (normalize_flags(flags), known[uid], folder))
            gone = sorted(set(known) - on_server)
            if gone:
                stats.gone += ctx.ingestor.mark_gone(ctx.account_id, folder, uids=gone)
            prune_fetch_failures(ctx.conn, ctx.account_id, folder, uidvalidity, server_uids)
            cur = {**cur, "aliases": sorted(aliases)}
            ctx.cursors.put(scope, cur)
        return added

    @staticmethod
    def _held_elsewhere(ctx: SyncContext, folder: str, loc: Location) -> bool:
        row = ctx.conn.execute(
            "select l.uid from message m join message_location l on l.message_id = m.id and l.folder = %s"
            " where m.account_id = %s and m.provider_key = %s", (folder, ctx.account_id, loc.provider_key)).fetchone()
        return bool(row) and row["uid"] != loc.uid and row["uid"] is not None
