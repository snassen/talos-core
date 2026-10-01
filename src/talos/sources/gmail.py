"""Gmail over IMAP, reading only All Mail so each message is fetched once.

Gmail's IMAP extensions give what plain IMAP cannot: a permanent message id
(X-GM-MSGID, used as the provider key), the thread id (X-GM-THRID) and every
label on the message (X-GM-LABELS, including \\Inbox and \\Sent). Labels are
server state, mirrored in message_location.labels; rules can read them.

Read-only by construction: the folder is selected with readonly=True and bodies
are fetched with BODY.PEEK[], which does not set \\Seen.

Cursor (scope 'all'): uidvalidity, last_uid and highestmodseq. New mail is found with
UID SEARCH last_uid+1:*. Flag and label changes on older mail come from CONDSTORE
(CHANGEDSINCE). If UIDVALIDITY ever changes, every UID is re-read; X-GM-MSGID keeps
that from creating duplicates.

No UID is lost: bodies are fetched in batches bounded by count and by RFC822.SIZE; a
UID the server leaves out of a FETCH is asked for again on its own, and one that still
does not come is listed in fetch_failure. Every run also catches up on UIDs at or below
the cursor that Talos neither holds nor lists as failed, so a gap left by an earlier
run (or an older version of this code) fills itself.
"""

from __future__ import annotations

import logging
from typing import Callable

from talos.ingest import Location
from talos.sources.base import (SyncContext, SyncStats, catch_up, chunks, fetch_complete, item, new_uids, normalize_flags,
                                prune_fetch_failures, raise_imap_line_limit, size_batches, uid_ref)

log = logging.getLogger("talos.sync.gmail")

FOLDER = "[all]"
BATCH = 100
FETCH = ["X-GM-MSGID", "X-GM-THRID", "X-GM-LABELS", "FLAGS", "INTERNALDATE", "BODY.PEEK[]"]
STATE_FETCH = ["X-GM-MSGID", "X-GM-LABELS", "FLAGS"]
MAX_BATCH_BYTES = None  # None: base.MAX_BATCH_BYTES

raise_imap_line_limit()


def _label(value) -> str:
    if isinstance(value, bytes):
        from imapclient import imap_utf7
        return imap_utf7.decode(value)
    return str(value)


_key = item


def _complete(d: dict) -> bool:
    """A body entry is data only if it carries what was asked for."""
    return isinstance(item(d, "BODY[]"), (bytes, bytearray)) and item(d, "X-GM-MSGID") is not None


class GmailSource:
    def __init__(self, address: str, password: Callable[[], str], *, host: str = "imap.gmail.com",
                 client_factory: Callable | None = None):
        self.address = address
        self.password = password
        self.host = host
        self.client_factory = client_factory

    # When more than this many changes (MODSEQ steps) piled up since the last sync, one SEARCH MODSEQ
    # over all of All Mail is too much for Gmail: it answers "System Error" and drops the connection
    # (seen after 55,000 Talos labels were written). Then the search goes window by window.
    MODSEQ_ONE_SEARCH = 5000
    MODSEQ_WINDOW = 2000

    def _changed_uids(self, client, since, modseq: int, server_uids: list[int]) -> list[int]:
        """The UIDs whose flags or labels changed since MODSEQ `since`."""
        if modseq - int(since) <= self.MODSEQ_ONE_SEARCH or not server_uids:
            return sorted(client.search(["MODSEQ", str(since)]))
        out: set[int] = set()
        lo, top = server_uids[0], server_uids[-1]
        while lo <= top:
            hi = min(lo + self.MODSEQ_WINDOW - 1, top)
            out.update(client.search(["UID", f"{lo}:{hi}", "MODSEQ", str(since)]))
            lo = hi + 1
        return sorted(out)

    def _connect(self):
        if self.client_factory:
            return self.client_factory()
        from imapclient import IMAPClient
        client = IMAPClient(self.host, ssl=True, timeout=120)
        client.normalise_times = False  # keep the server's timezone-aware INTERNALDATE
        client.login(self.address, self.password())
        return client

    def _all_mail(self, client) -> str:
        from imapclient.imapclient import ALL
        folder = client.find_special_folder(ALL)
        if not folder:
            raise RuntimeError("Gmail has no \\All folder over IMAP; is 'Show in IMAP' off for All Mail?")
        return folder

    def sync(self, ctx: SyncContext) -> SyncStats:
        stats = SyncStats()
        client = self._connect()
        try:
            folder = self._all_mail(client)
            info = client.select_folder(folder, readonly=True)
            uidvalidity = int(_key(info, "UIDVALIDITY"))
            modseq = _key(info, "HIGHESTMODSEQ")
            cur = ctx.cursors.get("all") or {}
            if cur.get("uidvalidity") != uidvalidity:
                if cur:
                    stats.notes.append("uidvalidity changed; re-reading all UIDs")
                cur = {"uidvalidity": uidvalidity, "last_uid": 0}

            # The full UID set tells what left All Mail and what an earlier run skipped;
            # new mail is what lies above the cursor.
            last = cur.get("last_uid", 0)
            server_uids = sorted(client.search(["ALL"]))
            missed = catch_up(ctx.conn, ctx.account_id, FOLDER, uidvalidity, server_uids, last)
            if missed:
                stats.notes.append(f"catching up on {len(missed)} UIDs below the cursor")
            todo = sorted(set(missed) | set(new_uids(client, last)))
            if ctx.limit is not None:
                todo = todo[: ctx.limit]
            stats.seen = len(server_uids)
            failed_refs = {r["ref"] for r in ctx.ingestor.fetch_failures(ctx.account_id)}

            for chunk in size_batches(client, todo, max_count=BATCH, max_bytes=MAX_BATCH_BYTES):
                data, missing = fetch_complete(client, chunk, FETCH, _complete)
                try:
                    with ctx.conn.transaction():
                        for uid in chunk:
                            ref = uid_ref(FOLDER, uidvalidity, uid)
                            if uid in missing:
                                ctx.ingestor.record_fetch_failure(
                                    ctx.account_id, ref, {"folder": FOLDER, "uidvalidity": uidvalidity, "uid": uid},
                                    "the server returned no body for this UID")
                                stats.failed += 1
                                continue
                            d = data[uid]
                            res = ctx.ingestor.ingest(ctx.account_id, item(d, "BODY[]"),
                                                      self._location(uid, uidvalidity, d))
                            stats.added += res.created
                            stats.updated += not res.created and not res.failed
                            stats.failed += res.failed
                            if ref in failed_refs:
                                ctx.ingestor.clear_fetch_failures(ctx.account_id, [ref])
                        cur = {**cur, "last_uid": max(last, max(chunk))}
                        last = cur["last_uid"]
                        ctx.cursors.put("all", cur)
                except Exception:
                    ctx.ingestor.reset_caches()
                    raise
                log.info("gmail batch uids=%d..%d added=%d", chunk[0], chunk[-1], stats.added)

            # Changes to flags and labels on mail we already have.
            since = cur.get("highestmodseq")
            if since and modseq and int(modseq) > int(since) and client.has_capability("CONDSTORE"):
                # SEARCH MODSEQ names the changed UIDs; IMAPClient cannot fetch a "1:*" range and
                # filter it (it maps the answer back through int()), so fetch those UIDs as a list.
                changed_uids = self._changed_uids(client, since, int(modseq), server_uids)
                changed = {}
                for part in chunks(changed_uids, 1000):
                    changed.update(client.fetch(part, STATE_FETCH))
                with ctx.conn.transaction():
                    for uid, d in changed.items():
                        msgid = item(d, "X-GM-MSGID")
                        if msgid is None or item(d, "FLAGS") is None:
                            continue  # not the entry asked for (an unsolicited FETCH keyed by sequence number)
                        row = ctx.conn.execute(
                            "select id from message where account_id = %s and provider_key = %s",
                            (ctx.account_id, str(msgid))).fetchone()
                        if row:
                            loc = self._location(uid, uidvalidity, d)
                            ctx.ingestor.observe(row["id"], ctx.account_id, loc)
                            stats.updated += 1

            # Anything we hold that All Mail no longer has was deleted or is in Trash/Spam.
            with ctx.conn.transaction():
                known = [r["uid"] for r in ctx.conn.execute(
                    "select uid from message_location where account_id = %s and folder = %s"
                    " and uidvalidity = %s and present", (ctx.account_id, FOLDER, uidvalidity))]
                gone = sorted(set(known) - set(server_uids))
                if gone:
                    stats.gone = ctx.ingestor.mark_gone(ctx.account_id, FOLDER, uids=gone)
                prune_fetch_failures(ctx.conn, ctx.account_id, FOLDER, uidvalidity, server_uids)
                if modseq:
                    cur = {**cur, "highestmodseq": int(modseq)}
                ctx.cursors.put("all", cur)
        finally:
            try:
                client.logout()
            except Exception:
                pass
        return stats

    @staticmethod
    def _location(uid: int, uidvalidity: int, d: dict) -> Location:
        msgid = str(_key(d, "X-GM-MSGID"))
        thrid = _key(d, "X-GM-THRID")
        return Location(
            folder=FOLDER,
            folder_path=FOLDER,
            provider_key=msgid,
            uidvalidity=uidvalidity,
            uid=uid,
            provider_id=msgid,
            flags=normalize_flags(_key(d, "FLAGS")),
            labels=sorted(_label(x) for x in (_key(d, "X-GM-LABELS") or ())),
            provider_thread_id=str(thrid) if thrid is not None else None,
            received_at=_key(d, "INTERNALDATE"),
        )
