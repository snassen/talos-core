"""Microsoft 365 mail over Microsoft Graph (not EWS, which Exchange Online retires).

Per mail folder, a delta query returns what is new, changed or removed since the
stored deltaLink. New messages are fetched as MIME ($value), so the vault holds the
same kind of original as for every other account. Immutable IDs (the Prefer header
below) make the provider key survive moves between folders.

Only GET requests are ever sent. Sign-in is a public client (device code), and the
token cache lives in the Keychain; see talos.graphauth.

Cursor (scope 'folder:<id>'): delta_link once a folder is caught up, or next_link
while its first pass is still running, so a long backfill resumes where it stopped.
The state also records the folder's readable path.

Locations are keyed by the Graph folder id (message_location.folder), which survives
a rename; the readable path is message_location.folder_path, kept up to date when a
folder is renamed, and what rules match. A folder that disappears from the folder
list has its locations marked gone and its cursor dropped. Hidden folders are skipped
unless the account says include_hidden_folders; skip_folders skips a folder and
everything under it.

One message never blocks a folder: a message deleted between the delta page and the
$value download (404) is simply gone, and any other download error is listed in
fetch_failure and retried at the start of the next run. When Graph says a delta
token is no longer valid (410, syncStateNotFound, resync required), the folder's
cursor is dropped and the folder is read again from the start; when that pass ends,
locations it did not see again are marked gone.
"""

from __future__ import annotations

import email.utils
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable

import httpx

from talos.ingest import Location
from talos.sources.base import SyncContext, SyncStats

log = logging.getLogger("talos.sync.graph")

GRAPH = "https://graph.microsoft.com/v1.0"
SELECT = "internetMessageId,conversationId,isRead,isDraft,flag,categories,parentFolderId,receivedDateTime"
FETCH_CONCURRENCY = 4  # Graph's limit on concurrent requests per mailbox
MAX_RETRIES = 6
MAX_WAIT = 300
RESYNC_CODES = {"syncstatenotfound", "syncstateinvalid", "resyncrequired"}
RETRY_FAILURES_PER_RUN = 200


class GraphError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code

    @property
    def resync(self) -> bool:
        """Graph no longer knows this delta or next link: the folder must be read again."""
        return (self.status == 410 or (self.code or "").lower() in RESYNC_CODES
                or "resync" in str(self).lower())


def _retry_after(value: str | None, attempt: int) -> float:
    """Retry-After is seconds or an HTTP date; anything unreadable falls back to backoff."""
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
        try:
            when = email.utils.parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, IndexError):
            pass
    return float(2 ** attempt)


def _error_code(resp: httpx.Response) -> str | None:
    try:
        return ((resp.json() or {}).get("error") or {}).get("code")
    except Exception:
        return None


class GraphClient:
    """GET-only Graph client: honours Retry-After (seconds or a date), retries network errors
    and 5xx, and on a 401 asks once for a fresh token."""

    def __init__(self, token: Callable[[], str], http: httpx.Client | None = None, *, sleep=time.sleep,
                 refresh: Callable[[], str] | None = None):
        self.token = token
        self.refresh = refresh or token
        self.http = http or httpx.Client(timeout=120)
        self.sleep = sleep
        self._token_lock = threading.Lock()  # MSAL and the Keychain are asked one thread at a time

    def get(self, url: str, *, raw: bool = False, page_size: int = 100):
        if url.startswith("/"):
            url = GRAPH + url
        prefer = f'IdType="ImmutableId", odata.maxpagesize={page_size}'
        with self._token_lock:
            token = self.token()
        refreshed = False
        attempt = 0
        while attempt < MAX_RETRIES:
            headers = {"Authorization": f"Bearer {token}", "Prefer": prefer}
            try:
                resp = self.http.get(url, headers=headers)
            except httpx.TransportError as exc:
                wait = float(2 ** attempt)
                log.warning("graph network error %s; retrying in %.0fs", type(exc).__name__, wait)
                self.sleep(min(wait, MAX_WAIT))
                attempt += 1
                continue
            if resp.status_code == 200:
                return resp.content if raw else resp.json()
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                with self._token_lock:
                    token = self.refresh()
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = _retry_after(resp.headers.get("Retry-After"), attempt)
                log.warning("graph throttled status=%d wait=%.0fs", resp.status_code, wait)
                self.sleep(min(wait, MAX_WAIT))
                attempt += 1
                continue
            raise GraphError(f"GET {url} -> {resp.status_code}: {resp.text[:300]}",
                             status=resp.status_code, code=_error_code(resp))
        raise GraphError(f"GET {url} still failing after {MAX_RETRIES} attempts")


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GraphMailSource:
    def __init__(self, client: GraphClient, *, skip_folders: set[str] | None = None,
                 include_hidden: bool = False):
        self.client = client
        self.skip = {s.strip("/").lower() for s in (skip_folders or set())}
        self.include_hidden = include_hidden

    def all_folders(self) -> list[dict]:
        """Every mail folder the mailbox has, hidden ones included, depth first, with a readable path."""
        out: list[dict] = []

        def walk(url: str, prefix: str, hidden_parent: bool) -> None:
            while url:
                page = self.client.get(url)
                for f in page.get("value", []):
                    path = f"{prefix}/{f['displayName']}" if prefix else f["displayName"]
                    hidden = hidden_parent or bool(f.get("isHidden"))
                    out.append({"id": f["id"], "path": path, "total": f.get("totalItemCount"), "hidden": hidden})
                    if f.get("childFolderCount"):
                        walk(f"/me/mailFolders/{f['id']}/childFolders?$top=100&includeHiddenFolders=true", path,
                             hidden)
                url = page.get("@odata.nextLink")

        walk("/me/mailFolders?$top=100&includeHiddenFolders=true", "", False)
        return out

    def _skipped(self, folder: dict) -> bool:
        if folder["hidden"] and not self.include_hidden:
            return True
        path = folder["path"].lower()
        return any(path == s or path.startswith(s + "/") for s in self.skip)

    def folders(self, listing: list[dict] | None = None) -> list[dict]:
        """The folders to sync."""
        return [f for f in (self.all_folders() if listing is None else listing) if not self._skipped(f)]

    def sync(self, ctx: SyncContext) -> SyncStats:
        stats = SyncStats()
        listing = self.all_folders()
        self._vanished(ctx, listing, stats)
        self._retry_fetch_failures(ctx, stats)
        remaining = ctx.limit
        for folder in self.folders(listing):
            if remaining is not None and remaining <= 0:
                stats.notes.append("limit reached; later folders not visited")
                break
            added = self._folder(ctx, folder, stats, remaining)
            if remaining is not None:
                remaining -= added
        return stats

    def _vanished(self, ctx: SyncContext, listing: list[dict], stats: SyncStats) -> None:
        """Folders Talos has synced that the mailbox no longer has: their mail is gone from there."""
        ids = {f["id"] for f in listing}
        if not ids:  # a mailbox always has an Inbox; an empty list is a bad answer, not an empty mailbox
            stats.notes.append("Graph listed no mail folders; nothing marked gone")
            return
        with ctx.conn.transaction():
            for scope, state in ctx.cursors.all().items():
                if not scope.startswith("folder:"):
                    continue
                folder_id = (state or {}).get("folder_id") or scope.split(":", 1)[1]
                if folder_id in ids:
                    continue
                n = ctx.ingestor.mark_folder_gone(ctx.account_id, folder_id)
                stats.gone += n
                ctx.conn.execute("delete from sync_cursor where account_id = %s and scope = %s",
                                 (ctx.account_id, scope))
                stats.notes.append(f"folder {(state or {}).get('path', folder_id)!r} no longer exists;"
                                   f" {n} locations marked gone")

    def _prefetch(self, ctx: SyncContext, items: list[dict]) -> dict[str, bytes | GraphError]:
        """Download the MIME of a page's new messages, FETCH_CONCURRENCY at a time.

        Graph allows four concurrent requests per mailbox; fetching one message at a time
        made a 133k-message backfill take about ten hours. Only the network work runs in
        threads: every database write stays on the sync's own connection, in order.
        """
        ids = [i["id"] for i in items if "@removed" not in i]
        if not ids:
            return {}
        held = {r["provider_key"] for r in ctx.conn.execute(
            "select provider_key from message where account_id = %s and provider_key = any(%s)",
            (ctx.account_id, ids))}
        todo = [i for i in ids if i not in held]
        out: dict[str, bytes | GraphError] = {}
        if not todo:
            return out
        with ThreadPoolExecutor(max_workers=FETCH_CONCURRENCY) as pool:
            futures = {mid: pool.submit(self.client.get, f"/me/messages/{mid}/$value", raw=True) for mid in todo}
            for mid, fut in futures.items():
                try:
                    out[mid] = fut.result()
                except GraphError as exc:
                    out[mid] = exc
        return out

    def _download(self, ctx: SyncContext, loc: Location, stats: SyncStats,
                  fetched: dict[str, bytes | GraphError] | None = None) -> bytes | None:
        """The message's MIME, or None when it could not be had (and that is recorded)."""
        try:
            got = (fetched or {}).get(loc.provider_id)
            if isinstance(got, GraphError):
                raise got
            if got is not None:
                return got
            return self.client.get(f"/me/messages/{loc.provider_id}/$value", raw=True)
        except GraphError as exc:
            if exc.status == 404:  # deleted after the delta page listed it: nothing to keep
                ctx.ingestor.clear_fetch_failures(ctx.account_id, [loc.provider_key])
                return None
            ctx.ingestor.record_fetch_failure(ctx.account_id, loc.provider_key, loc, exc)
            stats.failed += 1
            return None

    def _retry_fetch_failures(self, ctx: SyncContext, stats: SyncStats) -> None:
        """Messages whose MIME could not be downloaded on an earlier run; the delta has moved past them."""
        for r in ctx.ingestor.fetch_failures(ctx.account_id)[:RETRY_FAILURES_PER_RUN]:
            loc = Location.from_json(r["location"]) if r["location"].get("provider_key") else None
            if loc is None:
                continue
            try:
                with ctx.conn.transaction():
                    held = ctx.conn.execute("select 1 from message where account_id = %s and provider_key = %s",
                                            (ctx.account_id, loc.provider_key)).fetchone()
                    if held:
                        ctx.ingestor.clear_fetch_failures(ctx.account_id, [r["ref"]])
                        continue
                    raw = self._download(ctx, loc, stats)
                    if raw is None:
                        continue
                    res = ctx.ingestor.ingest(ctx.account_id, raw, loc)
                    stats.added += res.created
                    stats.failed += res.failed
                    if res.failed:  # it has a body now; ingest_failure holds it from here
                        ctx.ingestor.clear_fetch_failures(ctx.account_id, [r["ref"]])
            except Exception:
                ctx.ingestor.reset_caches()
                raise

    def _fresh(self, folder: dict) -> str:
        return f"/me/mailFolders/{folder['id']}/messages/delta?$select={SELECT}"

    def _folder(self, ctx: SyncContext, folder: dict, stats: SyncStats, limit: int | None) -> int:
        scope = f"folder:{folder['id']}"
        cur = ctx.cursors.get(scope) or {}
        if cur.get("path") and cur["path"] != folder["path"]:
            with ctx.conn.transaction():  # renamed or moved: the locations keep their key, the path follows
                ctx.conn.execute("update message_location set folder_path = %s where account_id = %s and folder = %s",
                                 (folder["path"], ctx.account_id, folder["id"]))
        url = cur.get("next_link") or cur.get("delta_link") or self._fresh(folder)
        resync_started = cur.get("resync_started")
        restarted = False
        added = 0
        while url:
            try:
                page = self.client.get(url)
            except GraphError as exc:
                if not exc.resync or restarted or url == self._fresh(folder):
                    raise
                # The delta token expired or Graph lost it: read the folder again from the start.
                restarted = True
                with ctx.conn.transaction():
                    resync_started = ctx.conn.execute("select clock_timestamp() as t").fetchone()["t"].isoformat()
                    ctx.cursors.put(scope, {"path": folder["path"], "folder_id": folder["id"],
                                            "resync_started": resync_started})
                cur = {}
                stats.notes.append(f"resync of {folder['path']} ({exc.code or exc.status})")
                url = self._fresh(folder)
                continue
            fetched = self._prefetch(ctx, page.get("value", []))
            try:
                with ctx.conn.transaction():
                    for item in page.get("value", []):
                        stats.seen += 1
                        if "@removed" in item:
                            stats.gone += ctx.ingestor.mark_gone(ctx.account_id, folder["id"],
                                                                 provider_ids=[item["id"]])
                            continue
                        loc = self._location(folder, item)
                        row = ctx.conn.execute(
                            "select id from message where account_id = %s and provider_key = %s",
                            (ctx.account_id, item["id"])).fetchone()
                        if row:
                            ctx.ingestor.observe(row["id"], ctx.account_id, loc)
                            stats.updated += 1
                            continue
                        raw = self._download(ctx, loc, stats, fetched)
                        if raw is None:
                            continue
                        res = ctx.ingestor.ingest(ctx.account_id, raw, loc)
                        if res.failed:
                            stats.failed += 1
                            continue
                        stats.added += 1
                        added += 1
                    nxt, delta = page.get("@odata.nextLink"), page.get("@odata.deltaLink")
                    state = {"path": folder["path"], "folder_id": folder["id"]}
                    if delta:
                        state["delta_link"] = delta
                        if resync_started:  # a full re-read ended: what it did not see is gone
                            stats.gone += ctx.ingestor.mark_folder_gone(ctx.account_id, folder["id"],
                                                                        observed_before=resync_started)
                    elif nxt:
                        state["next_link"] = nxt
                        if cur.get("delta_link"):
                            state["delta_link"] = cur["delta_link"]
                        if resync_started:
                            state["resync_started"] = resync_started
                    ctx.cursors.put(scope, state)
            except Exception:
                ctx.ingestor.reset_caches()
                raise
            url = nxt if not delta else None
            if limit is not None and added >= limit and url:
                stats.notes.append(f"limit reached inside {folder['path']}")
                break
        # A folder with nothing new is the usual case, every five minutes for every folder: debug only.
        (log.info if added else log.debug)("graph folder=%s added=%d", folder["path"], added)
        return added

    @staticmethod
    def _location(folder: dict, item: dict) -> Location:
        flags = []
        if item.get("isRead"):
            flags.append("seen")
        if (item.get("flag") or {}).get("flagStatus") == "flagged":
            flags.append("flagged")
        if item.get("isDraft"):
            flags.append("draft")
        return Location(
            folder=folder["id"],  # stable across renames; rules read folder_path
            folder_path=folder["path"],
            provider_key=item["id"],
            provider_id=item["id"],
            flags=flags,
            labels=list(item.get("categories") or []),
            provider_thread_id=item.get("conversationId"),
            received_at=_dt(item.get("receivedDateTime")),
        )
