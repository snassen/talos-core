"""Write-back to Microsoft 365 mail over Microsoft Graph (phase P3).

Two kinds of request write, PATCH and move, always inside JSON batches
(POST /$batch, at most 20 requests each):

    mark_read / mark_unread    PATCH /me/messages/{id}  {"isRead": true|false}
    flag / unflag              PATCH /me/messages/{id}  {"flag": {"flagStatus": ...}}
    add_label / remove_label   GET the categories, then PATCH the new list
                               (read-modify-write; only Talos/... may be added)
    archive                    POST /me/messages/{id}/move  {"destinationId": ...}
    trash                      move to the well-known folder "deleteditems"
    move (and undo of these)   move to a folder id, resolved from the readable path
                               through the sync cursors ({"path", "folder_id"})

Moving to Deleted Items is the furthest any operation goes. Nothing here sends
mail, deletes permanently or issues an HTTP DELETE. Throttling (429, and 503/504)
is honoured per request with Retry-After, and every message gets its own result.

Messages are addressed by their immutable id, which survives a move within the
mailbox, so the mirror's provider_id stays valid after trash and undo.

The token must carry Mail.ReadWrite (graphauth.WRITE_SCOPES). The owner consents to it once,
and the executor exists only for an account with writeback_enabled,
which is switched on for an agreed use and off afterwards (docs/writeback-test-plan.md).
Without the scope Graph answers 403 and each message is reported as failed.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Callable

import httpx

from talos.changesets import Target
from talos.sources.base import chunks
from talos.sources.graph import GRAPH

log = logging.getLogger("talos.writeback.graph")

BATCH_LIMIT = 20  # Graph's maximum number of requests in one JSON batch
MAX_RETRIES = 6
NAMESPACE = "Talos/"
PREFER = 'IdType="ImmutableId"'
WELL_KNOWN = {"inbox": "inbox", "archive": "archive", "deleteditems": "deleteditems"}
RETRYABLE = {429, 503, 504}


class GraphWriteError(RuntimeError):
    pass


def category_allowed(label: str | None) -> bool:
    return bool(label) and label.startswith(NAMESPACE) and len(label) > len(NAMESPACE)


def _header(headers: dict | None, name: str) -> str | None:
    for k, v in (headers or {}).items():
        if k.lower() == name.lower():
            return v
    return None


class GraphExecutor:
    def __init__(self, token: Callable[[], str], http: httpx.Client | None = None, *,
                 folders: dict[str, str] | None = None, archive_folder: str | None = None,
                 sleep=time.sleep, max_retries: int = MAX_RETRIES, readonly: bool = False):
        self.token = token
        self.http = http or httpx.Client(timeout=120)
        self.folders = dict(folders or {})  # readable path -> Graph folder id
        self.archive_folder = archive_folder
        self.sleep = sleep
        self.max_retries = max_retries
        # A read-only executor (dry runs and checks, with the read-only token) refuses apply().
        self.readonly = readonly
        # What the connection did, for the changeset's report: batches posted, requests in them,
        # throttled answers, seconds spent waiting on Retry-After, and seconds in HTTP.
        self.stats = {"batches": 0, "requests": 0, "throttled": 0, "waited_s": 0.0, "http_s": 0.0}
        self._well_known: dict[str, str] = {}

    def close(self) -> None:
        self.http.close()

    # -- the Executor protocol ----------------------------------------------------

    def apply(self, op: str, args: dict, targets: list[Target]) -> dict[int, str | None]:
        if self.readonly:
            raise GraphWriteError("this executor is read-only (a dry run); it cannot apply")
        unique: dict[int, Target] = {}
        for t in targets:
            unique.setdefault(t.message_id, t)
        results: dict[int, str | None] = {}
        usable = []
        for t in unique.values():
            if not t.provider_id:
                results[t.message_id] = "the mirror holds no Graph id for this message; sync first"
            else:
                usable.append(t)
        try:
            if op in ("add_label", "remove_label"):
                label = args.get("label")
                if not category_allowed(label):
                    return {**results, **{t.message_id: f"refused: Talos only writes categories under {NAMESPACE};"
                                                         f" {label!r} is not one" for t in usable}}
                results.update(self._categories(op, label, usable))
                return results
            request = self._request_for(op, args)
            if isinstance(request, str):
                return {**results, **{t.message_id: request for t in usable}}
            method, suffix, body = request
            reqs = {t.message_id: self._req(method, f"/me/messages/{t.provider_id}{suffix}", body) for t in usable}
            for mid, resp in self._batch(reqs).items():
                results[mid] = self._error(resp)
        except Exception as exc:
            log.warning("graph write failed op=%s: %s", op, exc)
            err = f"{type(exc).__name__}: {exc}"
            for t in usable:
                results.setdefault(t.message_id, err)
        return results

    def _request_for(self, op: str, args: dict) -> tuple[str, str, dict] | str:
        """(method, url suffix, body) for a single-shot op, or a refusal."""
        if op in ("mark_read", "mark_unread"):
            return "PATCH", "", {"isRead": op == "mark_read"}
        if op in ("flag", "unflag"):
            return "PATCH", "", {"flag": {"flagStatus": "flagged" if op == "flag" else "notFlagged"}}
        if op == "archive":
            dest = self.folders.get(self.archive_folder) if self.archive_folder else None
            return "POST", "/move", {"destinationId": dest or WELL_KNOWN["archive"]}
        if op == "trash":
            return "POST", "/move", {"destinationId": WELL_KNOWN["deleteditems"]}
        if op == "move":
            dest = self._folder_id(args.get("folder"))
            if dest is None:
                return (f"refused: cannot resolve folder {args.get('folder')!r} to a Graph folder id;"
                        " sync first so the cursors know it")
            return "POST", "/move", {"destinationId": dest}
        return f"refused: unknown operation {op!r}"

    def _folder_id(self, path: str | None) -> str | None:
        if not path:
            return None
        if path in self.folders:
            return self.folders[path]
        if path in self.folders.values():  # already a Graph folder id (message_location.folder)
            return path
        return WELL_KNOWN.get(path.lower())

    def _categories(self, op: str, label: str, targets: list[Target]) -> dict[int, str | None]:
        """Read-modify-write: categories are one list on the message, so read it first."""
        results: dict[int, str | None] = {}
        reads = {t.message_id: self._req("GET", f"/me/messages/{t.provider_id}?$select=categories")
                 for t in targets}
        writes = {}
        got = self._batch(reads)
        by_id = {t.message_id: t for t in targets}
        for mid, resp in got.items():
            err = self._error(resp)
            if err:
                results[mid] = err
                continue
            current = list((resp.get("body") or {}).get("categories") or [])
            if op == "add_label":
                new = current if label in current else current + [label]
            else:
                new = [c for c in current if c != label]
            if new == current:
                results[mid] = None  # already so; nothing to write
                continue
            writes[mid] = self._req("PATCH", f"/me/messages/{by_id[mid].provider_id}", {"categories": new})
        for mid, resp in self._batch(writes).items():
            results[mid] = self._error(resp)
        return results

    # -- reading the server: dry runs and checks after apply ----------------------------

    def _well_known_id(self, name: str) -> str | None:
        """The real id of a well-known folder (deleteditems, archive, inbox), read once."""
        if name not in self._well_known:
            got = self._batch({0: self._req("GET", f"/me/mailFolders/{name}?$select=id,displayName")}).get(0) or {}
            self._well_known[name] = ((got.get("body") or {}).get("id") if self._error(got) is None else None) or ""
        return self._well_known[name] or None

    def locate(self, targets: list[Target]) -> dict[int, dict]:
        """Where each message is on the server now: {folder_id, folder, subject, is_read} or {problem}.
        Only GET requests, in JSON batches."""
        paths = {v: k for k, v in self.folders.items()}
        reqs = {t.message_id: self._req("GET", f"/me/messages/{t.provider_id}?$select=id,subject,parentFolderId,isRead")
                for t in targets if t.provider_id}
        out = {t.message_id: {"problem": "the mirror holds no Graph id for this message; sync first"}
               for t in targets if not t.provider_id}
        for mid, resp in self._batch(reqs).items():
            err = self._error(resp)
            if err:
                out[mid] = {"problem": "not found on the server (moved away or deleted)" if resp.get("status") == 404 else err}
                continue
            b = resp.get("body") or {}
            out[mid] = {"folder_id": b.get("parentFolderId"), "folder": paths.get(b.get("parentFolderId"), b.get("parentFolderId")),
                        "subject": b.get("subject"), "is_read": b.get("isRead")}
        return out

    def destination(self, op: str, args: dict) -> str | None:
        """The folder id a move-like operation ends in, as the server names it."""
        if op == "trash":
            return self._well_known_id("deleteditems")
        if op == "archive":
            return self.folders.get(self.archive_folder) if self.archive_folder else self._well_known_id("archive")
        if op == "move":
            dest = self._folder_id(args.get("folder"))
            return self._well_known_id(dest) if dest in WELL_KNOWN else dest
        return None

    def settled(self, op: str, args: dict, targets: list[Target]) -> dict[int, bool]:
        """For trash, archive and move: whether each message is already where the operation puts it."""
        dest = self.destination(op, args)
        if not dest:
            return {}
        return {mid: loc.get("folder_id") == dest for mid, loc in self.locate(targets).items()}

    def dry_run(self, op: str, args: dict, targets: list[Target]) -> dict[int, dict]:
        """Check each message on the server and say exactly what apply() would send; sends nothing."""
        request = self._request_for(op, args) if op not in ("add_label", "remove_label") else (
            "PATCH", "", {"categories": "(current list with the label added or removed)"})
        where = self.locate(targets)
        dest = None
        if op == "trash":
            dest = self._well_known_id("deleteditems")
        out = {}
        by_id = {t.message_id: t for t in targets}
        for mid, loc in where.items():
            t = by_id[mid]
            if "problem" in loc:
                out[mid] = {"ok": False, "would": [], "server": {}, "problem": loc["problem"]}
                continue
            server = {"folder": loc["folder"], "read": loc["is_read"]}
            if isinstance(request, str):
                out[mid] = {"ok": False, "would": [], "server": server, "problem": request}
                continue
            if t.folder and loc["folder_id"] != t.folder and op in ("trash", "move", "archive"):
                out[mid] = {"ok": False, "would": [], "server": server,
                            "problem": f"the mirror has it elsewhere ({t.folder}); sync first"}
                continue
            if dest and loc["folder_id"] == dest:
                out[mid] = {"ok": False, "would": [], "server": server, "problem": "already in Deleted Items"}
                continue
            method, suffix, body = request
            out[mid] = {"ok": True, "server": server, "problem": None,
                        "would": [f"{method} /me/messages/{t.provider_id}{suffix} {json.dumps(body)}"
                                  f" (in a JSON batch of up to {BATCH_LIMIT})"]}
        return out

    # -- JSON batching ------------------------------------------------------------

    @staticmethod
    def _req(method: str, url: str, body: dict | None = None) -> dict:
        req = {"method": method, "url": url, "headers": {"Prefer": PREFER}}
        if body is not None:
            req["body"] = body
            req["headers"]["Content-Type"] = "application/json"
        return req

    @staticmethod
    def _error(resp: dict) -> str | None:
        status = resp.get("status", 0)
        if 200 <= status < 300:
            return None
        err = (resp.get("body") or {}).get("error") or {}
        return f"Graph {status}: {err.get('code', '')} {err.get('message', '')}".strip()

    def _batch(self, requests: dict[int, dict]) -> dict[int, dict]:
        """Send requests in batches of at most 20; retry throttled ones. message_id -> response."""
        out: dict[int, dict] = {}
        parts = list(chunks(list(requests.items()), BATCH_LIMIT))
        for n, part in enumerate(parts):
            pending = dict(part)
            for attempt in range(self.max_retries):
                if not pending:
                    break
                wait = 0.0
                try:
                    responses = self._post_batch(pending, attempt)
                except Exception as exc:
                    # Batches already answered keep their results: those requests were carried out.
                    # This one and the ones after it get the error, so the report never calls a
                    # message failed that the server has moved, nor moved that it has not.
                    log.warning("graph batch %d of %d failed: %s", n + 1, len(parts), exc)
                    err = {"status": 0, "body": {"error": {"code": type(exc).__name__, "message": str(exc)[:300]}}}
                    for later in [dict(part)] + [dict(p) for p in parts[n + 1:]]:
                        for mid in later:
                            if mid not in out:
                                out[mid] = err
                    return out
                if responses is None:  # the whole batch was throttled
                    continue
                for mid, resp in responses.items():
                    if resp.get("status") in RETRYABLE:
                        wait = max(wait, float(_header(resp.get("headers"), "Retry-After") or 2 ** attempt))
                        continue
                    out[mid] = resp
                    pending.pop(mid, None)
                if pending:
                    log.warning("graph throttled %d request(s); waiting %.0fs", len(pending), wait)
                    self.stats["throttled"] += len(pending)
                    self.stats["waited_s"] += min(wait, 300)
                    self.sleep(min(wait, 300))
            for mid in pending:
                out[mid] = {"status": 429, "body": {"error": {
                    "code": "throttled", "message": f"still throttled after {self.max_retries} attempts"}}}
        return out

    def _post_batch(self, pending: dict[int, dict], attempt: int) -> dict[int, dict] | None:
        body = {"requests": [{"id": str(mid), **req} for mid, req in pending.items()]}
        token = self.token()
        started = time.monotonic()
        resp = self.http.post(f"{GRAPH}/$batch", json=body,
                              headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        self.stats["http_s"] += time.monotonic() - started
        self.stats["batches"] += 1
        self.stats["requests"] += len(pending)
        if resp.status_code in RETRYABLE:
            wait = float(resp.headers.get("Retry-After", 2 ** attempt))
            log.warning("graph batch throttled status=%d wait=%.0fs", resp.status_code, wait)
            self.stats["throttled"] += len(pending)
            self.stats["waited_s"] += min(wait, 300)
            self.sleep(min(wait, 300))
            return None
        if resp.status_code != 200:
            raise GraphWriteError(f"POST /$batch -> {resp.status_code}: {resp.text[:300]}")
        got = {}
        for r in resp.json().get("responses", []):
            try:
                mid = int(r["id"])
            except (KeyError, ValueError):
                continue
            if mid in pending:
                got[mid] = r
        for mid in pending:
            got.setdefault(mid, {"status": 0, "body": {"error": {"code": "missing",
                                                                  "message": "no response in the batch"}}})
        return got
