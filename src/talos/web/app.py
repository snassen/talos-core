"""The local web app: a JSON API over the archive and a static UI.

It listens on 127.0.0.1 only; other devices on the tailnet reach it through `tailscale
serve`, and only as an allowed tailnet user (TailnetIdentity, TALOS_HOME/web.json). Because any web page in the browser can try to reach
localhost, two more guards apply: requests must carry a localhost Host header
(defeats DNS rebinding), and attachments are served under a sandbox CSP and as
downloads unless they are PDFs or images, so a malicious HTML attachment can never
run script in the app's origin. A message's own HTML is cleaned (talos.mailhtml) and served
from its own endpoint under a CSP with no script, for a sandboxed frame; remote images only
when asked. Mailbox changes go through changesets from the CLI (the
UI shows them and can dry-run one, which opens the mailbox read-only and sends nothing).

Most writes stay inside Talos's own database: objects and binders, work items, rules, planned
structure changesets (prepared, never committed or applied), the owner's importance marks, answer-key
labels, acceptance thresholds, discovery decisions, Studio decisions and lifts, watchers, drafts
and signatures. Three kinds of route reach a server outside Talos, each only on the owner's action:
- sending a mail they composed (/api/drafts/{id}/send), and posting to Teams (/api/teams/send): each
  needs a one-time confirmation token that the confirm route issues for that exact message
  (/api/drafts/{id}/confirm, /api/teams/confirm), and both must come from this page (X-Talos and
  Sec-Fetch-Site: same-origin); talos.send is the only code that sends;
- saving a calendar entry in a calendar that lives elsewhere (Microsoft 365, iCloud, Google), through
  talos.calwrite, and only where that calendar can be written (/api/calendar/entries…);
- Sync now (/api/sync, /api/teams/refresh, /api/calendar/sync), which only reads.
Every POST must carry the X-Talos header, except Argus's check-ins (/argus/checkin, /argus/fail),
which come from scripts over curl and carry the check-in token instead (talos.argus, docs/argus.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, quote

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from talos import (acceptance, activity, argus, boundary, calendars, calwrite, clusters, compose, config, db, discover, discovery, facets, gold,
                   importance, mailhtml, objects, rules, search, space, structure, suggest, syncnow, timeline, unlock, work)
from talos import manual, personal, releases, studio, sureness, webauth
from talos import send as sending  # the one importer of talos.send: its routes need a fresh confirmation
from talos.vault import BlobUnavailable, Vault
from talos.web import gate

STATIC = Path(__file__).parent / "static"
INLINE_TYPES = ("application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp")


def _default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (bytes, memoryview)):
        return None
    raise TypeError(type(o).__name__)


class JSON(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(content, default=_default, ensure_ascii=False).encode("utf-8")


def content_disposition(kind: str, filename: str) -> str:
    """A Content-Disposition header that any filename survives.

    HTTP headers are Latin-1, so a name with "–" or "€" used to raise and give a 500. The
    header now carries an ASCII fallback plus the exact name as RFC 5987 UTF-8. Control
    characters (CR and LF above all) are removed, so a name can never start a new header."""
    name = re.sub(r"[\x00-\x1f\x7f]+", " ", filename).strip() or "attachment"
    fallback = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    fallback = " ".join(re.sub(r'["\\]', "", fallback).split()) or "attachment"
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def _bool(v: str | None) -> bool | None:
    return None if v in (None, "") else v in ("1", "true", "yes")


def _refused(request: Request) -> JSONResponse | None:
    # A custom header forces a CORS preflight, which this app never answers, so no other
    # web page open in the browser can post here.
    if request.headers.get("x-talos") != "1":
        return JSON({"error": "missing X-Talos header"}, status_code=403)
    return None


def _teams_target(body: dict) -> dict:
    """A Teams post's target and text from a request: a thread (a chat, or a channel thread) or a team and channel."""
    text = body.get("text")
    if not isinstance(text, str):
        raise ValueError("the text is text")
    if body.get("thread_id"):
        return {"thread_id": int(body["thread_id"]), "text": text}
    return {"team": str(body.get("team") or ""), "channel": str(body.get("channel") or ""), "text": text}


def _refused_send(request: Request) -> JSONResponse | None:
    """Confirming and sending also need the browser's own word that this page made the request:
    Sec-Fetch-Site is set by the browser, never by page script, and is same-origin only for this app."""
    if refused := _refused(request):
        return refused
    if request.headers.get("sec-fetch-site") != "same-origin":
        return JSON({"error": "sending is done from the compose pane in Talos"}, status_code=403)
    return None


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise objects.ObjectError("the body must be JSON")
    if not isinstance(body, dict):
        raise objects.ObjectError("the body must be a JSON object")
    return body


def _filters(p) -> dict:
    """The Messages filters from a query string, shared by the list, the senders and the facets
    (search.filters_from; a watcher or an aggregation reads its stored query the same way)."""
    return search.filters_from(p.get, p.getlist)


RULE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}")


def _slug(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:60] or "rule"


def _rule(body: dict, existing: dict | None) -> rules.Rule:
    """A Rule from a form, or a RuleError that says what is wrong with it."""
    rid = str(body.get("id") or "").strip().lower() or _slug(str(body.get("name") or ""))
    if not RULE_ID.fullmatch(rid):
        raise rules.RuleError("the id may hold only a-z, 0-9, '.', '_' and '-', and at most 80 characters")
    name = str(body.get("name") or "").strip()
    if not name:
        raise rules.RuleError("the rule needs a name")
    conditions = body.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        raise rules.RuleError("the rule needs at least one condition")
    action = body.get("action")
    if not isinstance(action, dict):
        raise rules.RuleError("action must be {dimension, value} or {object}")
    if action.get("object") not in (None, ""):
        try:
            action = {"object": int(action["object"])}
        except (TypeError, ValueError):
            raise rules.RuleError("the object is an object id, a number")
    elif action.get("dimension"):
        action = {"dimension": str(action["dimension"]), "value": str(action.get("value") or "").strip()}
    else:
        raise rules.RuleError("action must be {dimension, value} or {object}")
    try:
        priority = int(body.get("priority", existing["priority"] if existing else 100))
    except (TypeError, ValueError):
        raise rules.RuleError("priority is a whole number; lower runs first")
    description = str(body.get("description") or "").strip() or None
    enabled = body.get("enabled", existing["enabled"] if existing else True)
    return rules.Rule(id=rid, name=name, conditions=conditions, action=action, priority=priority,
                      enabled=bool(enabled), description=description)


def _work_fields(body: dict, *, creating: bool) -> dict:
    """The work item fields from a form, typed; a WorkError says what is wrong with one."""
    allowed = set(work.EDITABLE) | ({"message_ids"} if creating else set())
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise work.WorkError(f"cannot set {', '.join(unknown)}")
    out = dict(body)
    if "title" in out:
        if not isinstance(out["title"], str):
            raise work.WorkError("the title is text")
        out["title"] = out["title"].strip()
    if "body" in out:
        if out["body"] is None:
            out["body"] = ""
        if not isinstance(out["body"], str):
            raise work.WorkError("the body is text (Markdown)")
    if "status" in out and not isinstance(out["status"], str):
        raise work.WorkError(f"unknown status {out['status']!r}; use one of {', '.join(work.STATUSES)}")
    if "focus" in out and not isinstance(out["focus"], bool):
        raise work.WorkError("focus is true or false")
    if "home_id" in out:
        if out["home_id"] in (None, ""):
            out["home_id"] = None
        else:
            try:
                out["home_id"] = int(out["home_id"])
            except (TypeError, ValueError):
                raise work.WorkError("the home is an object id, a number") from None
    if "position" in out:
        if isinstance(out["position"], bool) or not isinstance(out["position"], (int, float)):
            raise work.WorkError("position is a number")
        out["position"] = float(out["position"])
    if "message_ids" in out:
        ids = out["message_ids"] or []
        if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            raise work.WorkError("message_ids is a list of message ids")
    return out


def _ids(body: dict, key: str) -> list[int]:
    ids = body.get(key) or []
    if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise work.WorkError(f"{key} is a list of message ids")
    return ids


_STATIC_REF = re.compile(r'(["\'])/static/([A-Za-z0-9._/-]+)\1')


def _fingerprinted(page: str = "index.html") -> str:
    """The page (index.html, login.html) with ?v=<digest> on every /static/ file it names that exists."""
    root = STATIC.resolve()

    def ref(m: re.Match) -> str:
        path = (STATIC / m.group(2)).resolve()
        if root not in path.parents or not path.is_file():
            return m.group(0)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        return f"{m.group(1)}/static/{m.group(2)}?v={digest}{m.group(1)}"

    return _STATIC_REF.sub(ref, (STATIC / page).read_text(encoding="utf-8"))


ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
_NETLOC = re.compile(r"[A-Za-z0-9.\-]+(:\d{1,5})?|\[[0-9A-Fa-f:.]+\](:\d{1,5})?")


def _image_base(request: Request) -> str:
    """This app's attachment URL prefix, for the mail frame's img-src. The Host header passed
    TrustedHostMiddleware, which checks only the name before the colon, so the port is checked
    here too: nothing but a host and a port may reach the CSP header."""
    netloc = request.url.netloc
    if not _NETLOC.fullmatch(netloc):
        return "'self'"
    return f"{'https' if request.url.scheme == 'https' else 'http'}://{netloc}/api/attachments/"


class TailnetIdentity:
    """Requests that arrive under a tailnet host name must come from an allowed tailnet user.

    `tailscale serve` proxies them to 127.0.0.1 and sets Tailscale-User-Login to the
    signed-in user of the calling device (it replaces any such header a client sends).
    A request under a tailnet name without an allowed login gets 403; localhost requests
    are unaffected. The tailnet names themselves still have to pass TrustedHostMiddleware.
    """

    def __init__(self, app, *, hosts: list[str], users: list[str]):
        self.app = app
        self.hosts = {h.lower() for h in hosts}
        self.users = {u.lower() for u in users}

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            host = headers.get("host", "").split(":")[0].lower()
            if host in self.hosts and headers.get("tailscale-user-login", "").lower() not in self.users:
                await JSON({"error": "this tailnet user may not use Talos"}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


ARGUS_BODY_MAX = 4096


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() != "bearer ":
        return None
    return auth[7:].strip() or None


def _truth(v) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "ok"):
        return True
    if s in ("0", "false", "no"):
        return False
    raise argus.ArgusError("ok is true or false")


async def _argus_params(request: Request) -> dict:
    """A check-in's fields from the query string and the body (form-encoded or JSON), at most 4 kB.
    The token is never taken from the body."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > ARGUS_BODY_MAX:
            raise argus.ArgusError("the body is larger than 4 kB")
    params: dict = dict(request.query_params)
    if body.strip():
        if request.headers.get("content-type", "").split(";")[0].strip() == "application/json":
            try:
                data = json.loads(body)
            except ValueError:
                raise argus.ArgusError("the body is not valid JSON") from None
            if not isinstance(data, dict):
                raise argus.ArgusError("the body must be a JSON object")
        else:
            data = dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))
        params.update(data)
    params.pop("token", None)
    return params


def create(settings: config.Settings | None = None, *, allowed_hosts: list[str] | None = None,
           dry_runners=None, argus_token=None, argus_notifier=None, beat_configured=None,
           argus_timers: bool | None = None, send_probe=None, send_transport=None, send_book=None,
           sync_runner=None, auth_store=None, auth_notifier=None, reads=None, calendar_source=None, calendar_writers=None, teams_transport=None,
           calendar_readiness=None) -> Starlette:
    """The app. allowed_hosts defaults to localhost only; tests pass their own test host.

    argus_token (a function returning the check-in token), argus_notifier and beat_configured replace
    the Keychain and macOS in tests; argus_timers overrides TALOS_HOME/argus.json's "enabled".
    send_probe (compose.Probe), send_transport (account row → transport) and send_book
    (send.Confirmations) replace the Keychain, the mail servers and the clock in tests and the demo.
    sync_runner replaces subprocess.Popen for Sync now in tests; calendar_source (account row → a
    calendar source) replaces Microsoft Graph for the calendar's Refresh; calendar_writers ({kind: writer})
    and calendar_readiness ({kind: (ok, note)}) replace the calendar servers for writing.

    Signing in (web.gate, talos.webauth) is enforced always, with the credentials in the Keychain. The
    one exception is an app made for the test host alone ("testserver") without an auth_store: the
    older tests talk to the API directly. Tests of the door pass a webauth.MemoryStore."""
    settings = settings or config.load()
    vault = Vault(settings.vault)
    probe = send_probe or compose.Probe()
    sender = sending.Sender(probe, transport_for_account=send_transport or sending.transport_for, book=send_book,
                            **({"teams_transport": teams_transport} if teams_transport else {}))

    def conn():
        return db.connect(settings.dsn)

    sync_now = syncnow.SyncNow(settings.home, **({"runner": sync_runner} if sync_runner else {}))
    teams_fast = syncnow.TeamsFast(settings.home, **({"runner": sync_runner} if sync_runner else {}))
    testing = allowed_hosts == ["testserver"]
    enforce = not (testing and auth_store is None)
    store = auth_store if auth_store is not None else webauth.KeychainStore()
    read_budget = reads or webauth.ReadBudget()
    door_notify = auth_notifier if auth_notifier is not None else (None if testing else argus.Notifier())

    def overview(request: Request):
        with conn() as c:
            return JSON(search.overview_cached(c, dsn=settings.dsn, refresh=bool(_bool(request.query_params.get("refresh")))))

    def messages(request: Request):
        p = request.query_params
        with conn() as c:
            return JSON(search.messages(c, **_filters(p), limit=int(p.get("limit", 50)),
                                        offset=int(p.get("offset", 0)),
                                        sort="importance" if p.get("sort") == "importance" else None))

    def threads(request: Request):
        # Conversations: one row per thread, with the Messages filters; a thread matches when any
        # of its messages does.
        p = request.query_params
        with conn() as c:
            return JSON(search.threads(c, **_filters(p), limit=_int(request, "limit", 50, search.THREAD_MAX_LIMIT),
                                       offset=max(0, int(p.get("offset", 0) or 0))))

    def thread_detail(request: Request):
        # One conversation, oldest first; ?before=<message id>&limit=… pages back through a long chat.
        # ?around=<message id> opens it with that message in the middle; ?after=<id> pages forward.
        p = request.query_params
        try:
            before, after, around = (int(p[k]) if p.get(k) else None for k in ("before", "after", "around"))
        except ValueError:
            return JSON({"error": "before, after and around are message ids"}, status_code=400)
        with conn() as c:
            t = search.thread(c, int(request.path_params["id"]), before=before, after=after, around=around,
                              limit=_int(request, "limit", 100, search.THREAD_DETAIL_MAX))
        return JSON(t) if t else JSON({"error": "no such conversation"}, status_code=404)

    def senders(request: Request):
        # limit=0 or all=1: every sender, up to facets.MAX_SENDERS.
        p = request.query_params
        limit = 0 if _bool(p.get("all")) else int(p.get("limit") or 20)
        with conn() as c:
            return JSON(facets.senders(c, **_filters(p), limit=limit))

    def domains(request: Request):
        # The sender domains, like senders(): limit=0 or all=1 for every one of them.
        p = request.query_params
        limit = 0 if _bool(p.get("all")) else int(p.get("limit") or 20)
        with conn() as c:
            return JSON(facets.domains(c, **_filters(p), limit=limit))

    def facet_values(request: Request):
        # The three facets are independent; each gets its own connection so they run at once.
        f = _filters(request.query_params)

        def one(name):
            with conn() as c:
                return name, facets.facet(c, name, **dict(f))

        with ThreadPoolExecutor(len(facets.FACETS)) as pool:
            return JSON(dict(pool.map(one, facets.FACETS)))

    def top_senders(request: Request):
        p = request.query_params
        with conn() as c:
            return JSON(facets.top_senders(c, days=int(p["days"]) if p.get("days") else None,
                                           people_only=_bool(p.get("people_only")) is not False,
                                           limit=int(p.get("limit") or 15)))

    def message(request: Request):
        with conn() as c:
            m = search.message(c, int(request.path_params["id"]))
            if m:
                m["importance"] = importance.detail(c, m["id"])
                m["work"] = work.of_message(c, m["id"])
                # Whether the message has HTML to show in a frame, and what the frame blocks. The
                # page never gets the HTML itself: the frame loads it from /html.
                try:
                    m["html"] = mailhtml.summary(c, vault, m["id"])
                except Exception:  # a removed original, a message the parser cannot read: text only
                    m["html"] = None
        return JSON(m) if m else JSON({"error": "no such message"}, status_code=404)

    def message_html(request: Request):
        # The message's own HTML, cleaned, for <iframe sandbox> (talos.mailhtml): a CSP with no
        # script, no fetch and no form; remote images only with ?images=1; no referrer sent.
        allow = bool(_bool(request.query_params.get("images")))
        with conn() as c:
            try:
                clean = mailhtml.render(c, vault, int(request.path_params["id"]), allow_remote=allow)
            except BlobUnavailable:
                return Response("The original was removed or blocked by the Mac's security software.",
                                status_code=410, media_type="text/plain")
        if clean is None:
            return Response("this message has no HTML", status_code=404, media_type="text/plain")
        return Response(clean.html, media_type="text/html", headers={
            "Content-Security-Policy": mailhtml.csp(_image_base(request), allow_remote=allow),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store"})

    def message_raw(request: Request):
        with conn() as c:
            try:
                found = search.original(c, vault, int(request.path_params["id"]))
            except BlobUnavailable:
                return Response("The original was removed or blocked by the Mac's security software.",
                                status_code=410, media_type="text/plain")
        if found is None:
            return Response("no such message", status_code=404)
        data, kind = found
        media, suffix = ("application/json", "json") if kind == "json" else ("message/rfc822", "eml")
        return Response(data, media_type=media, headers={
            "Content-Disposition": content_disposition("attachment", f"talos-{request.path_params['id']}.{suffix}"),
            "Content-Security-Policy": "sandbox"})

    # An attachment from the vault. Mail is untrusted input (promise 6): only PDFs and images open in
    # the page; anything else downloads. Every answer carries a sandbox CSP and nosniff, so an HTML or
    # SVG attachment can never run script in the app's origin. It counts as a read for the bulk guard.
    def attachment(request: Request):
        with conn() as c:
            a = c.execute("select blob_sha256, filename, content_type from attachment where id = %s",
                          (int(request.path_params["id"]),)).fetchone()
        if not a:
            return Response("no such attachment", status_code=404)
        if a["blob_sha256"] is None:  # a Teams file link: the file stays in SharePoint/OneDrive
            return Response("this attachment is a link and was not downloaded", status_code=404)
        ctype = a["content_type"] if a["content_type"] in INLINE_TYPES else "application/octet-stream"
        disp = "inline" if ctype in INLINE_TYPES else "attachment"
        try:
            data = vault.get(a["blob_sha256"], "attachment")
        except BlobUnavailable:
            return Response("This file was removed or blocked by the Mac's security software"
                            " (it was probably malicious).", status_code=410, media_type="text/plain")
        return Response(data, media_type=ctype, headers={
            "Content-Disposition": content_disposition(disp, a["filename"] or "attachment"),
            "Content-Security-Policy": "sandbox",
            "X-Content-Type-Options": "nosniff"})

    def changesets(request: Request):
        # Every open changeset and the newest 200 finished ones, each with the status to show, its
        # group (open or history), its undo links and its operations counted by status.
        from talos import changesets as cs_mod
        with conn() as c:
            rows = c.execute(
                "select c.id, c.title, c.status, c.summary #- '{dry_run,items}' as summary, c.note, c.created_at,"
                " c.planned_at, c.committed_at,"
                " c.finished_at, (c.selection->>'undo_of')::bigint as undo_of, c.request->>'op' as op,"
                " coalesce((select jsonb_object_agg(x.status, x.n) from (select o.status, count(*) as n"
                "           from changeset_op o where o.changeset_id = c.id group by 1) x), '{}') as ops"
                " from changeset c where c.status = any(%s) or c.id in"
                " (select id from changeset where not status = any(%s) order by id desc limit 200)"
                " order by c.id desc", (list(cs_mod.OPEN), list(cs_mod.OPEN))).fetchall()
            return JSON(cs_mod.lifecycle(rows))

    def changeset_detail(request: Request):
        cid = int(request.path_params["id"])
        with conn() as c:
            cs = c.execute("select id, title, status, selection, request, summary, note, created_at, planned_at,"
                           " committed_at, finished_at from changeset where id = %s", (cid,)).fetchone()
            if not cs:
                return JSON({"error": "no such changeset"}, status_code=404)
            cs["ops"] = c.execute(
                "select o.message_id, m.subject, m.from_address, o.account_id, o.op, o.args, o.status, o.error,"
                " o.inverse, o.applied_at from changeset_op o join message m on m.id = o.message_id"
                " where o.changeset_id = %s order by o.id limit 200", (cid,)).fetchall()
            # A large changeset's dry run lists thousands of messages: the pane gets the problems and
            # the first 50, and how many there were.
            dr = (cs["summary"] or {}).get("dry_run")
            if dr and len(dr.get("items") or []) > 50:
                items = dr["items"]
                dr["items_total"] = len(items)
                dr["items"] = [i for i in items if not i.get("ok")][:50] + [i for i in items if i.get("ok")][:50]
                dr["items"] = dr["items"][:100]
            cs["op_counts"] = {r["status"]: r["n"] for r in c.execute(
                "select status, count(*)::int as n from changeset_op where changeset_id = %s group by 1", (cid,))}
            # Its place in the lifecycle: the changeset it undoes, and the newest undo made of it.
            from talos import changesets as cs_mod
            cs["undo_of"] = (cs["selection"] or {}).get("undo_of")
            kin = c.execute("select id, status, (selection->>'undo_of')::bigint as undo_of from changeset"
                            " where id = %s or selection->>'undo_of' = %s", (cid, str(cid))).fetchall()
            me = next(r for r in cs_mod.lifecycle(kin) if r["id"] == cid)
            cs.update(shown=me["shown"], group=me["group"], undone_by=me["undone_by"],
                      undone_by_status=me["undone_by_status"])
            return JSON(cs)

    def changeset_dry_run(request: Request):
        # Runs in the thread pool (a plain def): it opens Gmail read-only and sends nothing.
        if refused := _refused(request):
            return refused
        from talos import changesets as cs_mod
        from talos.writeback import close_all, dry_runners_for
        cid = int(request.path_params["id"])
        with conn() as c:
            runners = dry_runners(c) if dry_runners else dry_runners_for(c)
            try:
                return JSON(cs_mod.dry_run(c, cid, runners))
            except cs_mod.ChangesetError as exc:
                return JSON({"error": str(exc)}, status_code=400)
            finally:
                close_all(runners)

    def structure_state(request: Request):
        # The Structure page: per account, the target tree with its counts, the inbox before and
        # after, and the changesets made from the plan. Read-only: nothing here reaches a mailbox.
        try:
            srules, rules_error = structure.load(), None
        except structure.StructureError as exc:
            srules, rules_error = None, str(exc)
        with conn() as c:
            rep = structure.report(c)
            accounts = []
            names = list(srules.accounts) if srules else []
            for acc in names + [a for a in rep if a not in names]:
                r = rep.get(acc)
                provider = c.execute("select provider from account where id = %s", (acc,)).fetchone()
                live = {x["target"]: x for x in (r["live"] if r else [])}
                counts = {k: {"count": v["n"], "in_inbox": v["in_inbox"], "leaves_inbox": v["leaves_inbox"]}
                          for k, v in live.items()}
                before = sum(v["in_inbox"] for v in live.values())
                leaving = sum(v["leaves_inbox"] for v in live.values())
                full = (r or {}).get("full_run") or {}
                accounts.append({
                    "account": acc, "name": srules.accounts[acc].name if srules and acc in srules.accounts else acc,
                    "provider": provider["provider"] if provider else None, "planned": bool(live),
                    "total": sum(v["n"] for v in live.values()), "inbox_before": before, "inbox_after": before - leaving,
                    "to_sort": live.get(structure.TO_SORT, {}).get("n", 0),
                    "left_alone": sum(v["n"] for v in live.values() if v["place"] == "leave"),
                    "computed_at": max((v["computed_at"] for v in live.values()), default=None),
                    "full_run": full, "last_run": (r or {}).get("last_run"),
                    "stale_rules": bool(srules and full and full.get("rules_sha") != srules.sha),
                    "old": ((r or {}).get("summary") or {}).get("old", []),
                    "same_place": ((r or {}).get("summary") or {}).get("same_place"),
                    "tree": structure.tree(srules, acc, counts) if srules and acc in srules.accounts else [],
                    "changesets_possible": bool(provider and provider["provider"] == "gmail")})
            return JSON({"rules": {"version": srules.version, "sha": srules.sha, "path": srules.path} if srules else None,
                         "rules_error": rules_error, "accounts": accounts, "changesets": structure.prepared(c)})

    def structure_examples(request: Request):
        p = request.query_params
        if not p.get("account") or not p.get("target"):
            return JSON({"error": "account and target are required"}, status_code=400)
        with conn() as c:
            return JSON(structure.examples(c, p["account"], p["target"], rule=p.get("rule") or None,
                                           limit=_int(request, "limit", 25, 200), offset=max(0, int(p.get("offset") or 0))))

    def structure_checklist(request: Request):
        # "How to make it real": per account, each step from the plan to the mailbox with its live
        # state (the plan, the mirror's Talos labels, the changesets waiting). Reads only.
        try:
            srules = structure.load()
        except structure.StructureError as exc:
            return JSON({"error": str(exc)}, status_code=400)
        with conn() as c:
            return JSON({"accounts": structure.checklist(c, srules)})

    async def structure_prepare(request: Request):
        # Prepare the next structure changesets for one Gmail account, as `talos structure changesets`
        # does: planned only, never committed or applied. Writes changeset rows in Talos, nothing to
        # a mailbox. body: {"account": "gmail", "limit": 1 | 10 | 100 | null (all)}.
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
        except objects.ObjectError as exc:
            return JSON({"error": str(exc)}, status_code=400)
        account, limit = body.get("account"), body.get("limit")
        if not isinstance(account, str) or not account:
            return JSON({"error": "which account? {\"account\": \"gmail\"}"}, status_code=400)
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
            return JSON({"error": "limit is a whole number of at least 1, or null for all"}, status_code=400)

        def prepare():
            with conn() as c:
                return structure.changesets(c, account, limit=limit, rules=structure.load())

        try:
            made = await run_in_threadpool(prepare)
        except structure.StructureError as exc:
            return JSON({"error": str(exc)}, status_code=400)
        sets = [m for m in made if "id" in m]
        return JSON({"made": sets, "notes": [m["note"] for m in made if "note" in m],
                     "will_change": sum(m["will_change"] for m in sets)}, status_code=201 if sets else 200)

    def structure_why(request: Request):
        with conn() as c:
            try:
                return JSON(structure.why(c, int(request.path_params["id"])))
            except structure.StructureError as exc:
                return JSON({"error": str(exc)}, status_code=404 if str(exc).startswith("no message") else 400)

    def rule_list(request: Request):
        with conn() as c:
            # One grouped count per table, joined to the rules, instead of two correlated counts
            # per rule: on the real archive that was 38 scans of 1.16M edges and took 8 s.
            return JSON(c.execute(
                "with a as (select source_ref as ref, count(*) as n from assignment"
                "           where source_kind = 'rule' and status = 'active' group by 1),"
                "     e as (select source as ref, count(*) as n from edge where source like 'rule:%%' group by 1)"
                " select r.*, coalesce(a.n, 0) + coalesce(e.n, 0) as hits from rule r"
                " left join a on a.ref = 'rule:' || r.id || '@' || r.version"
                " left join e on e.ref = 'rule:' || r.id || '@' || r.version"
                " order by r.priority, r.id").fetchall())

    async def rule_preview(request: Request):
        if refused := _refused(request):
            return refused
        body = await request.json()
        with conn() as c:
            try:
                return JSON(rules.preview(c, body.get("conditions", [])))
            except rules.RuleError as exc:
                return JSON({"error": str(exc)}, status_code=400)

    async def rule_save(request: Request):
        # Create (body "new": true refuses an id that exists) or update. A change to the
        # conditions or the action makes a new version; the values follow at the next run.
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
        except objects.ObjectError as exc:
            return JSON({"error": str(exc)}, status_code=400)
        with conn() as c:
            try:
                rid = str(body.get("id") or "").strip().lower()
                existing = c.execute("select * from rule where id = %s", (rid,)).fetchone() if rid else None
                rule = _rule(body, existing)
                if existing is None:
                    existing = c.execute("select * from rule where id = %s", (rule.id,)).fetchone()
                    if existing and body.get("new"):
                        raise rules.RuleError(f"a rule with the id {rule.id!r} exists already; choose another id")
                before = existing["version"] if existing else None
                rules.save(c, rule)
            except rules.RuleError as exc:
                c.rollback()
                return JSON({"error": str(exc)}, status_code=400)
            row = c.execute("select * from rule where id = %s", (rule.id,)).fetchone()
            return JSON({"rule": row, "created": before is None,
                         "new_version": before is not None and row["version"] != before},
                        status_code=201 if before is None else 200)

    async def rule_enabled(request: Request):
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
        except objects.ObjectError as exc:
            return JSON({"error": str(exc)}, status_code=400)
        if not isinstance(body.get("enabled"), bool):
            return JSON({"error": 'the body must be {"enabled": true} or {"enabled": false}'}, status_code=400)
        with conn() as c:
            row = c.execute("update rule set enabled = %s, updated_at = now() where id = %s returning *",
                            (body["enabled"], request.path_params["id"])).fetchone()
        return JSON(row) if row else JSON({"error": "no such rule"}, status_code=404)

    suggestion_cache = suggest.Cache()

    def rule_suggestions(request: Request):
        # Rules drafted from the accepted values (talos.suggest), grouped by category, all off. Without
        # ?category: the categories and their counts. With it (and ?value): those suggestions, a page at a
        # time, each with its preview count and example subjects, the largest coverage first (?sort=agree
        # for the most agreeing). Drafting takes seconds on the archive; it is cached until the values
        # change. Nothing is created here.
        p = request.query_params
        with conn() as c:
            rows = suggest.without_existing(c, suggestion_cache.get(c))
            head = {"threshold": suggest.MIN_MESSAGES, "computed_at": suggestion_cache.at,
                    "seconds": suggestion_cache.seconds, "total": len(rows)}
            if not p.get("category"):
                return JSON({**head, "categories": suggest.categories(rows)})
            cover = suggestion_cache.cover
            mine = [r for r in rows if r["category"] == p["category"] and (not p.get("value") or r["value"] == p["value"])]
            mine = suggest.search(mine, p.get("q") or "")
            if p.get("sort") == "agree":
                mine.sort(key=lambda r: (-r["agree"], r["sender"], r["skeleton"] or "", r["dimension"]))
            else:
                mine.sort(key=lambda r: (-cover.get(r["id"], 0), -r["agree"], r["sender"], r["skeleton"] or ""))
            offset, limit = max(0, int(p.get("offset") or 0)), _int(request, "limit", 25, 100)
            return JSON({**head, "category": p["category"], "value": p.get("value"), "count": len(mine),
                         "offset": offset, "rows": suggest.details(c, mine[offset:offset + limit])})

    def rule_suggestion_tree(request: Request):
        # The drill-down: field → category → value, each with its number of suggestions and the messages
        # they would cover, and the rules already made from each value's suggestions. The suggestions
        # themselves only when ?q filters them (by sender, subject pattern or value); otherwise a value's
        # suggestions are fetched when it is opened (rule_suggestions with ?category and ?value).
        q = (request.query_params.get("q") or "").strip()
        with conn() as c:
            rows = suggest.without_existing(c, suggestion_cache.get(c))
            shown = suggest.search(rows, q)
            fields = suggest.tree(c, shown, suggestion_cache.cover)
        if not q:
            for f in fields:
                for cat in f["categories"]:
                    for v in cat["values"]:
                        v["suggestions"] = []
        else:  # searching: a value with only rules made from it stays when the value itself matches
            ql = q.lower()
            hit = lambda v: v["suggestions"] or ql in v["value"].lower() or ql in v["label"].lower()
            for f in fields:
                for cat in f["categories"]:
                    cat["values"] = [v for v in cat["values"] if hit(v)]
                f["categories"] = [cat for cat in f["categories"] if cat["values"]]
            fields = [f for f in fields if f["categories"]]
        return JSON({"threshold": suggest.MIN_MESSAGES, "computed_at": suggestion_cache.at,
                     "seconds": suggestion_cache.seconds, "total": len(rows), "shown": len(shown), "q": q,
                     "fields": fields})

    def _group_targets(c, body: dict, rows: list[dict]) -> list[tuple[str, str]]:
        if body.get("category"):
            got = suggest.category_values(rows, str(body["category"]))
            if not got:
                # a category whose suggestions are all covered already: its values' group rules, if any
                raise rules.RuleError("no suggestions left in this category")
            return got
        if body.get("dimension") and body.get("value"):
            return [(str(body["dimension"]), str(body["value"]))]
        raise rules.RuleError('the body must be {"category": key} or {"dimension": d, "value": v}')

    async def rule_group_preview(request: Request):
        # What ticking a value (one rule) or a category (one rule per value) would save: each rule's
        # conditions, the messages it matches, and how many of them carry another accepted value.
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
        except objects.ObjectError as exc:
            return JSON({"error": str(exc)}, status_code=400)

        def run():
            with conn() as c:
                try:
                    rows = suggest.without_existing(c, suggestion_cache.get(c))
                    out = [suggest.group_preview(c, suggest.group_rule(c, rows, d, v))
                           for d, v in _group_targets(c, body, rows)]
                except rules.RuleError as exc:
                    return JSON({"error": str(exc)}, status_code=400)
                return JSON({"rules": out, "count": sum(r["count"] for r in out),
                             "conflicts": sum(r["conflicts"] for r in out)})

        return await run_in_threadpool(run)

    async def rule_group_add(request: Request):
        # Save the ticked value's (or each value of the ticked category's) group rule, switched off.
        # Its members stop being suggested; removing the rule brings them back. The rules are not re-run.
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
        except objects.ObjectError as exc:
            return JSON({"error": str(exc)}, status_code=400)

        def run():
            with conn() as c:
                try:
                    rows = suggest.without_existing(c, suggestion_cache.get(c))
                    made = []
                    for d, v in _group_targets(c, body, rows):
                        rule, p = suggest.add_group(c, rows, d, v)
                        made.append({**p, "version": rule.version})
                except rules.RuleError as exc:
                    c.rollback()
                    return JSON({"error": str(exc)}, status_code=400)
                return JSON({"rules": made}, status_code=201)

        return await run_in_threadpool(run)

    async def rule_suggestion_add(request: Request):
        # Save one suggestion as a rule, switched off. The rules are not re-run: an off rule sets nothing.
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
        except objects.ObjectError as exc:
            return JSON({"error": str(exc)}, status_code=400)

        def add():
            with conn() as c:
                found = next((r for r in suggestion_cache.get(c) if r["id"] == body.get("id")), None)
                if not found:
                    return JSON({"error": "no such suggestion (the values may have changed: reload the page)"},
                                status_code=404)
                if c.execute("select 1 from rule where id = %s", (found["id"],)).fetchone():
                    return JSON({"error": f"the rule {found['id']} exists already"}, status_code=409)
                try:
                    suggest.add(c, found)
                except rules.RuleError as exc:
                    c.rollback()
                    return JSON({"error": str(exc)}, status_code=400)
                return JSON({"rule": c.execute("select * from rule where id = %s", (found["id"],)).fetchone()},
                            status_code=201)

        return await run_in_threadpool(add)

    async def rule_remove(request: Request):
        # Remove a rule (after the owner's confirmation in the page): its values and memberships go, so each
        # message falls back to its next source; the definition is kept in rule_removed with the why.
        # A rule made from suggestions makes them suggestions again.
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
        except objects.ObjectError as exc:
            return JSON({"error": str(exc)}, status_code=400)
        with conn() as c:
            try:
                res = rules.remove(c, request.path_params["id"], why=str(body.get("why") or "") or None)
            except rules.RuleError as exc:
                return JSON({"error": str(exc)}, status_code=404)
        suggestion_cache.invalidate()
        return JSON(res)

    def rule_removed(request: Request):
        with conn() as c:
            return JSON(rules.removed(c, limit=_int(request, "limit", 50, 500)))

    def rule_run(request: Request):
        # Recompute every rule-made value, as `talos rules run` does, under the same lock, so a
        # click never runs alongside the scheduled sync's rule run. Takes seconds on the archive.
        if refused := _refused(request):
            return refused
        from talos.cli import _single

        with _single(settings, "rules") as ok:
            if not ok:
                return JSON({"error": "the rules are being run already; try again in a moment"}, status_code=409)
            started = time.monotonic()
            with conn() as c:
                counts = rules.run_all(c)
            return JSON({"counts": counts, "assigned": sum(counts.values()), "rules": len(counts),
                         "seconds": round(time.monotonic() - started, 2)})

    def dimensions(request: Request):
        # For the rule editor: each dimension, its allowed values, and the values in use.
        with conn() as c:
            dims = c.execute("select id, label, cardinality, allowed, description from dimension order by id").fetchall()
            used = c.execute("select dimension_id, value, count(*) as n from assignment where status = 'active'"
                             " group by 1, 2 order by 3 desc").fetchall()
        for d in dims:
            d["values"] = [u["value"] for u in used if u["dimension_id"] == d["id"]][:200]
        return JSON(dims)

    def object_list(request: Request):
        with conn() as c:
            rows = objects.list_objects(c, include_archived=bool(_bool(request.query_params.get("archived"))))
            open_work = {r["home_id"]: r["n"] for r in c.execute(
                "select home_id, count(*) n from work_item where status <> 'done' and home_id is not null group by 1")}
        for o in rows:
            o["work_open"] = open_work.get(o["id"], 0)
        return JSON(rows)

    def object_detail(request: Request):
        oid, p = int(request.path_params["id"]), request.query_params
        recursive = bool(_bool(p.get("recursive")))
        with conn() as c:
            obj = objects.get(c, oid)
            if not obj:
                return JSON({"error": "no such object"}, status_code=404)
            try:
                obj["members"] = objects.members(c, oid, p.get("kind") or None, limit=int(p.get("limit", 100)),
                                                 offset=int(p.get("offset", 0)), recursive=recursive)
            except objects.ObjectError as exc:
                return JSON({"error": str(exc)}, status_code=400)
            if _bool(p.get("common")) is not False:  # common=0 when only paging the members
                obj["common"] = objects.common(c, oid, recursive=recursive)
                obj["excluded"] = objects.excluded(c, oid)
                obj["parents"] = objects.objects_of(c, oid)
                obj["work"] = work.list_items(c, home_id=oid)
                obj["notes"] = activity.notes(c, oid)
                obj["facts"] = activity.facts(c, oid)
            return JSON(obj)

    def object_activity(request: Request):
        # The binder's timeline, newest first; ?show=all|work|messages|notes, ?before=<next of the page before>.
        oid, p = int(request.path_params["id"]), request.query_params
        with conn() as c:
            if not objects.get(c, oid):
                return JSON({"error": "no such object"}, status_code=404)
            try:
                return JSON(activity.activity(c, oid, show=p.get("show") or "all", before=p.get("before") or None,
                                              limit=_int(request, "limit", 100, activity.MAX_LIMIT)))
            except activity.ActivityError as exc:
                return JSON({"error": str(exc)}, status_code=400)

    async def object_create(request: Request):
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
            with conn() as c:
                if body.get("promote_org"):
                    oid = objects.promote_org(c, int(body["promote_org"]), kind=body.get("kind") or "project")
                else:
                    oid = objects.create(c, body.get("kind") or "collection", body.get("name") or "",
                                         description=body.get("description"), query=body.get("query"))
                return JSON(objects.get(c, oid), status_code=201)
        except (objects.ObjectError, ValueError, TypeError) as exc:
            return JSON({"error": str(exc)}, status_code=400)

    async def object_update(request: Request):
        if refused := _refused(request):
            return refused
        oid = int(request.path_params["id"])
        try:
            body = await _body(request)
            with conn() as c:
                if not objects.get(c, oid):
                    return JSON({"error": "no such object"}, status_code=404)
                if "name" in body:
                    objects.rename(c, oid, body["name"])
                if "description" in body:
                    objects.describe(c, oid, body["description"])
                if "body" in body:
                    objects.set_body(c, oid, body["body"])
                if "query" in body:
                    objects.set_query(c, oid, body["query"])
                if "archived" in body:
                    objects.archive(c, oid, bool(body["archived"]))
                if "starts_on" in body or "ends_on" in body:
                    cur = objects.get(c, oid)
                    objects.set_dates(c, oid, starts_on=body.get("starts_on", cur["starts_on"]),
                                      ends_on=body.get("ends_on", cur["ends_on"]))
                return JSON(objects.get(c, oid))
        except objects.ObjectError as exc:
            return JSON({"error": str(exc)}, status_code=400)

    async def object_members(request: Request):
        if refused := _refused(request):
            return refused
        oid = int(request.path_params["id"])
        actions = {"add": objects.add, "remove": objects.remove, "exclude": objects.exclude,
                   "unexclude": objects.unexclude}
        try:
            body = await _body(request)
            action, ids = body.get("action"), body.get("ids")
            if action not in actions:
                raise objects.ObjectError(f"action must be one of {', '.join(actions)}")
            if not isinstance(ids, list) or not ids:
                raise objects.ObjectError("ids must be a non-empty list of entity ids")
            with conn() as c:
                if not objects.get(c, oid):
                    return JSON({"error": "no such object"}, status_code=404)
                changed = actions[action](c, oid, [int(i) for i in ids])
                return JSON({"action": action, "changed": changed,
                             "counts": objects.members(c, oid, limit=0)["counts"]})
        except (objects.ObjectError, ValueError, TypeError) as exc:
            return JSON({"error": str(exc)}, status_code=400)

    # ---- importance (talos.importance): Today's lists, and the owner's own mark.
    def _int(request: Request, name: str, default: int, most: int) -> int:
        try:
            return max(1, min(int(request.query_params.get(name, default)), most))
        except ValueError:
            return default

    def importance_today(request: Request):
        with conn() as c:
            return JSON(importance.today(c, limit=_int(request, "limit", 5, 50), hours=_int(request, "hours", 24, 24 * 7)))

    def importance_waiting(request: Request):
        with conn() as c:
            return JSON(importance.waiting(c, limit=_int(request, "limit", 10, 100), days=_int(request, "days", 30, 366)))

    def importance_check(request: Request):
        with conn() as c:
            return JSON(importance.check(c, limit=_int(request, "limit", 5, 50)))

    async def importance_mark(request: Request):
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
        except objects.ObjectError as exc:
            return JSON({"error": str(exc)}, status_code=400)
        if "value" not in body:
            return JSON({"error": 'the body needs "value": "important", "not_important" or null'}, status_code=400)
        with conn() as c:
            try:
                res = importance.mark(c, int(request.path_params["id"]), body["value"])
            except rules.RuleError as exc:
                return JSON({"error": str(exc)}, status_code=400)
        return JSON(res) if res else JSON({"error": "no such message"}, status_code=404)

    # ---- work items (talos.work): the board, the list, the drawer and "Needs attention".
    def work_list(request: Request):
        p = request.query_params
        statuses = [s for v in p.getlist("status") for s in v.split(",") if s]
        home = p.get("home") or None
        if home not in (None, "none"):
            try:
                home = int(home)
            except ValueError:
                return JSON({"error": "home is an object id, or none"}, status_code=400)
        with conn() as c:
            try:
                return JSON(work.list_items(c, status=statuses or None, home_id=home, focus=_bool(p.get("focus")),
                                            q=p.get("q") or None, overdue=_bool(p.get("overdue"))))
            except work.WorkError as exc:
                return JSON({"error": str(exc)}, status_code=400)

    def work_attention(request: Request):
        with conn() as c:
            return JSON(work.attention(c, limit=_int(request, "limit", 100, 500)))

    def work_homes(request: Request):
        # For the home pickers: every object that can be a home, without counting members.
        with conn() as c:
            return JSON(c.execute(
                "select o.id, o.kind, o.name, (select count(*) from work_item w where w.home_id = o.id"
                " and w.status <> 'done') as work_open from object o where not o.archived"
                " order by lower(o.name), o.id").fetchall())

    def work_detail(request: Request):
        with conn() as c:
            item = work.get(c, int(request.path_params["id"]))
        return JSON(item) if item else JSON({"error": "no such work item"}, status_code=404)

    async def work_create(request: Request):
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
            fields = _work_fields(body, creating=True)
            if "title" not in fields:
                raise work.WorkError("a work item needs a title")
            with conn() as c:
                wid = work.create(c, fields.pop("title"), **{k: v for k, v in fields.items() if k != "position"})
                return JSON(work.get(c, wid), status_code=201)
        except (work.WorkError, objects.ObjectError) as exc:
            return JSON({"error": str(exc)}, status_code=400)

    async def work_update(request: Request):
        # A board move is {status, position}; a status without a position goes to the end of
        # its column. Unchanged fields write no history.
        if refused := _refused(request):
            return refused
        wid = int(request.path_params["id"])
        try:
            body = await _body(request)
            fields = _work_fields(body, creating=False)
            with conn() as c:
                row = c.execute("select status from work_item where id = %s", (wid,)).fetchone()
                if not row:
                    return JSON({"error": "no such work item"}, status_code=404)
                if fields.get("status") not in (None, row["status"]) and "position" not in fields \
                        and fields["status"] in work.STATUSES:
                    fields["position"] = work.end_position(c, fields["status"])
                work.update(c, wid, **fields)
                return JSON(work.get(c, wid))
        except (work.WorkError, objects.ObjectError) as exc:
            return JSON({"error": str(exc)}, status_code=400)

    async def work_messages(request: Request):
        if refused := _refused(request):
            return refused
        wid = int(request.path_params["id"])
        try:
            body = await _body(request)
            add, remove = _ids(body, "add"), _ids(body, "remove")
            if not add and not remove:
                raise work.WorkError('the body is {"add": [message ids], "remove": [message ids]}')
            with conn() as c:
                if not c.execute("select 1 from work_item where id = %s", (wid,)).fetchone():
                    return JSON({"error": "no such work item"}, status_code=404)
                return JSON({"messages": work.change_messages(c, wid, add=add, remove=remove)})
        except (work.WorkError, objects.ObjectError) as exc:
            return JSON({"error": str(exc)}, status_code=400)

    # ---- the calendar (talos.calendars): Talos's own calendars, edited here, and read-only copies of
    # the Microsoft 365 ones. The copy is refreshed by the 5-minute sync and by Refresh on the page.
    def calendar_state(request: Request):
        q = request.query_params
        try:
            start = date.fromisoformat(q.get("start") or date.today().isoformat())
            end = date.fromisoformat(q.get("end") or start.isoformat())
            with conn() as c:
                cals = calendars.list_calendars(c)
                found = calendars.entries(c, start, end)
                calwrite.annotate(c, cals, found, calendar_readiness)
                return JSON({"calendars": cals, "entries": found, "synced": calendars.last_sync(c),
                             "syncs": calendars.syncs(c)})
        except (calendars.CalendarError, ValueError) as exc:
            return JSON({"error": str(exc)}, status_code=400)

    def timeline_state(request: Request):
        q = request.query_params
        try:
            start = date.fromisoformat(q.get("start") or date.today().isoformat())
            end = date.fromisoformat(q.get("end") or start.isoformat())
            with conn() as c:
                return JSON(timeline.timeline(c, start, end, kind=q.get("kind") or None,
                                              binder=int(q["binder"]) if q.get("binder") else None,
                                              done=q.get("done") == "1", focus=q.get("focus") == "1"))
        except (timeline.TimelineError, ValueError) as exc:
            return JSON({"error": str(exc)}, status_code=400)

    async def _calendar_write(request: Request, fn):
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)

            def run():
                with conn() as c:
                    out = fn(c, body)
                    c.commit()
                    return out
            return JSON(await run_in_threadpool(run))
        except (calendars.CalendarError, TypeError) as exc:
            return JSON({"error": str(exc)}, status_code=400)
        except Exception as exc:  # a calendar server that did not answer: say so, never a bare 500
            return JSON({"error": f"the calendar did not take it: {type(exc).__name__}: {exc}"[:300]}, status_code=502)

    def _entry_fields(body: dict) -> dict:
        return {k: body[k] for k in calendars.EDITABLE if k in body}

    # Saving an entry in a real calendar talks to its server (talos.calwrite), so it runs in the thread pool.
    def _annotated(c, entry: dict) -> dict:
        """The saved entry as the page gets it from GET /api/calendar: whether it can be edited or removed."""
        calwrite.annotate(c, calendars.list_calendars(c), [entry], calendar_readiness)
        return entry

    async def calendar_entry_create(request: Request):
        return await _calendar_write(request, lambda c, b: _annotated(c, calwrite.create_entry(
            c, writers=calendar_writers, **_entry_fields(b),
            **({"work_item_id": int(b["work_item_id"])} if b.get("work_item_id") else {}))))

    async def calendar_entry_update(request: Request):
        eid = int(request.path_params["id"])

        def change(c, b):
            if "removed" in b:
                calwrite.remove_entry(c, eid, removed=bool(b["removed"]), writers=calendar_writers)
                return {"id": eid, "removed": bool(b["removed"])}
            return _annotated(c, calwrite.update_entry(c, eid, writers=calendar_writers, **_entry_fields(b)))
        return await _calendar_write(request, change)

    async def calendar_create(request: Request):
        return await _calendar_write(request, lambda c, b: calendars.create_calendar(
            c, b.get("name") or "", color=int(b["color"]) if b.get("color") else None))

    async def calendar_update(request: Request):
        cid = int(request.path_params["id"])

        def change(c, b):
            if b.get("is_default") and not (calendar_readiness or calwrite.can_write(c, cid)):
                raise calendars.CalendarError("Talos cannot write to that calendar yet, so it cannot take new entries")
            return calendars.update_calendar(
                c, cid, name=b.get("name"), color=int(b["color"]) if b.get("color") is not None else None,
                visible=b.get("visible"), is_default=b.get("is_default"))
        return await _calendar_write(request, change)

    async def calendar_sync(request: Request):
        if refused := _refused(request):
            return refused

        def run():
            with conn() as c:
                return calendars.sync_all(c, **({"source_factory": calendar_source} if calendar_source else {}))
        return JSON({"accounts": await run_in_threadpool(run)})

    # ---- the answer key (talos.gold): blind labelling. An item never carries a value Talos
    # decided (a rule, the pre-pass, a model, importance) nor why it was picked; those show only in
    # the reveal after a finished round, which the server refuses before that.
    def gold_sets(request: Request):
        with conn() as c:
            return JSON({"sets": gold.sets(c)})

    def gold_set(request: Request):
        with conn() as c:
            try:
                sid = int(request.path_params["id"])
                st = gold.check_state(c, sid)
                return JSON({"progress": gold.progress(c, sid), "options": gold.options(c),
                             "check": {"total": st["total"], "checked": st["checked"]} if st else None})
            except gold.GoldError as exc:
                return JSON({"error": str(exc)}, status_code=404)

    def gold_item(request: Request):
        with conn() as c:
            try:
                return JSON(gold.item(c, int(request.path_params["id"]), int(request.path_params["pos"])))
            except gold.GoldError as exc:
                return JSON({"error": str(exc)}, status_code=404)

    async def gold_label(request: Request):
        # {field, values, status, duration_ms}: one answer, saved at once; an empty note removes it.
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
        except objects.ObjectError as exc:
            return JSON({"error": str(exc)}, status_code=400)
        with conn() as c:
            try:
                return JSON(gold.save_label(c, int(request.path_params["id"]), int(request.path_params["pos"]),
                                            body.get("field"), body.get("values") or [], body.get("status") or "set",
                                            body.get("duration_ms")))
            except (gold.GoldError, TypeError, ValueError) as exc:
                return JSON({"error": str(exc)}, status_code=400)

    def gold_reveal(request: Request):
        # A POST: the first reveal of a round is recorded, so a later change to its answers is flagged.
        if refused := _refused(request):
            return refused
        with conn() as c:
            try:
                return JSON(gold.reveal(c, int(request.path_params["id"]), int(request.path_params["round"])))
            except gold.GoldError as exc:
                return JSON({"error": str(exc)}, status_code=409)

    # ---- the check (talos.gold): the owner checks a frozen sample of Claude's answers. Not blind, by
    # design; these endpoints are the only ones that carry another labeller's answers.
    def gold_check(request: Request):
        with conn() as c:
            sid = int(request.path_params["id"])
            try:
                st = gold.check_state(c, sid)
                if not st:
                    return JSON({"error": f"answer key {sid} has no check sample; draw one with:"
                                          f" talos enrich gold check-sample --set {sid}"}, status_code=404)
                return JSON({"state": st, "summary": gold.check_summary(c, sid), "options": gold.options(c)})
            except gold.GoldError as exc:
                return JSON({"error": str(exc)}, status_code=404)

    def gold_check_item(request: Request):
        with conn() as c:
            try:
                return JSON(gold.check_item(c, int(request.path_params["id"]), int(request.path_params["rank"])))
            except gold.GoldError as exc:
                return JSON({"error": str(exc)}, status_code=404)

    async def gold_check_agree(request: Request):
        # {fields: [...], duration_ms}: Claude's answer for these fields becomes theirs.
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
        except objects.ObjectError as exc:
            return JSON({"error": str(exc)}, status_code=400)
        with conn() as c:
            try:
                return JSON(gold.agree(c, int(request.path_params["id"]), int(request.path_params["rank"]),
                                       body.get("fields"), body.get("duration_ms")))
            except (gold.GoldError, TypeError, ValueError) as exc:
                return JSON({"error": str(exc)}, status_code=400)

    def gold_check_summary(request: Request):
        with conn() as c:
            summary = gold.check_summary(c, int(request.path_params["id"]))
            return JSON(summary) if summary else JSON({"error": "no check sample"}, status_code=404)

    # ---- the acceptance explorer (talos.acceptance): what a threshold gives, per field. The page
    # recomputes its numbers from the answer key's cases and the archive's histograms; the
    # examples are asked when the slider stops; apply is accept() over every backfill run.
    def acceptance_state(request: Request):
        p = request.query_params
        with conn() as c:
            try:
                return JSON(acceptance.state(c, p.get("gold_run") or None, refresh=bool(_bool(p.get("refresh")))))
            except (boundary.BoundaryError, ValueError) as exc:
                return JSON({"error": str(exc)}, status_code=409)

    def acceptance_examples(request: Request):
        p = request.query_params
        try:
            t = float(p.get("t", ""))
        except ValueError:
            return JSON({"error": "t is a threshold from 0 to 1"}, status_code=400)
        with conn() as c:
            try:
                return JSON(acceptance.examples(c, p.get("field") or "", t))
            except ValueError as exc:
                return JSON({"error": str(exc)}, status_code=400)

    async def acceptance_apply(request: Request):
        # {thresholds: {field: t}, dry_run}: accept over every backfill and focused run.
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
        except objects.ObjectError as exc:
            return JSON({"error": str(exc)}, status_code=400)
        with conn() as c:
            try:
                res = acceptance.apply(c, body.get("thresholds"), dry_run=bool(body.get("dry_run")))
                c.commit()
                return JSON(res)
            except (ValueError, boundary.BoundaryError) as exc:
                return JSON({"error": str(exc)}, status_code=400)

    # ---- composing and sending (talos.compose, talos.send). Drafts and signatures are Talos's own;
    # the send route is the only one that reaches a mail server (Teams posts have their own, further
    # down), and only with a confirmation.
    def _draft_view(c, d: dict) -> dict:
        sigs = compose.signatures(c)
        accounts = compose.senders(c, probe)
        original = None
        if d["original_id"]:
            original = c.execute("select id, subject, from_name, from_address, account_id, received_at from message"
                                 " where id = %s", (d["original_id"],)).fetchone()
        return {"draft": d, "senders": accounts, "signatures": sigs,
                "suggested": {a["id"]: (s["id"] if (s := compose.signature_for(c, a["id"], d["mode"])) else None)
                              for a in accounts},
                "warnings": compose.warnings(c, d), "original": original,
                "rate": {"sent_last_hour": sending.sends_in_window(c), "limit": sending.RATE_LIMIT}}

    def compose_senders(request: Request):
        with conn() as c:
            return JSON({"senders": compose.senders(c, probe), "default": compose.get_setting(c, "default_account"),
                         "from_name": compose.from_name(c), "from_name_set": compose.get_setting(c, "from_name")})

    async def compose_settings(request: Request):
        # {default_account: id or null, from_name: text or null}: only the keys given change.
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
            with conn() as c:
                if "default_account" in body:
                    compose.set_default_account(c, probe, body["default_account"] or None)
                if "from_name" in body:
                    name = " ".join(str(body["from_name"] or "").split())[:120]
                    compose.set_setting(c, "from_name", name or None)
                c.commit()
                return JSON({"senders": compose.senders(c, probe), "default": compose.get_setting(c, "default_account"),
                             "from_name": compose.from_name(c), "from_name_set": compose.get_setting(c, "from_name")})
        except (compose.ComposeError, objects.ObjectError) as exc:
            return JSON({"error": str(exc)}, status_code=400)

    def compose_suggest(request: Request):
        with conn() as c:
            return JSON(compose.suggest(c, request.query_params.get("q") or "", limit=_int(request, "limit", 8, 20)))

    def signature_list(request: Request):
        with conn() as c:
            return JSON({"signatures": compose.signatures(c), "senders": compose.senders(c, probe),
                         "send_log": c.execute("select id, at, account_id, from_addr, to_addrs, cc_addrs, bcc_addrs,"
                                               " subject, result, detail from send_log order by id desc limit 20").fetchall()})

    async def signature_save(request: Request):
        if refused := _refused(request):
            return refused
        sid = request.path_params.get("id")
        try:
            body = await _body(request)
            with conn() as c:
                row = compose.save_signature(c, body, int(sid) if sid is not None else None)
                c.commit()
                return JSON(row, status_code=200 if sid is not None else 201)
        except (compose.ComposeError, objects.ObjectError) as exc:
            return JSON({"error": str(exc)}, status_code=400)

    def signature_remove(request: Request):
        if refused := _refused(request):
            return refused
        with conn() as c:
            ok = compose.remove_signature(c, int(request.path_params["id"]))
            c.commit()
        return JSON({"removed": True}) if ok else JSON({"error": "no such signature"}, status_code=404)

    def draft_list(request: Request):
        with conn() as c:
            return JSON(compose.drafts(c))

    async def draft_create(request: Request):
        # {mode: new | reply | reply_all | forward, message_id}: a draft, prefilled for an answer.
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
            mid = body.get("message_id")
            if mid is not None and (isinstance(mid, bool) or not isinstance(mid, int)):
                raise compose.ComposeError("message_id is a message id")
            with conn() as c:
                d = compose.start(c, probe, body.get("mode") or "new", mid)
                c.commit()
                return JSON(_draft_view(c, d), status_code=201)
        except (compose.ComposeError, objects.ObjectError) as exc:
            return JSON({"error": str(exc)}, status_code=400)

    def draft_detail(request: Request):
        with conn() as c:
            d = compose.get_draft(c, int(request.path_params["id"]))
            return JSON(_draft_view(c, d)) if d else JSON({"error": "no such draft"}, status_code=404)

    async def draft_save(request: Request):
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
            with conn() as c:
                d = compose.save_draft(c, int(request.path_params["id"]), body)
                c.commit()
                return JSON({"draft": d, "warnings": compose.warnings(c, d)})
        except (compose.ComposeError, objects.ObjectError) as exc:
            return JSON({"error": str(exc)}, status_code=400)

    def draft_discard(request: Request):
        if refused := _refused(request):
            return refused
        with conn() as c:
            ok = compose.discard(c, int(request.path_params["id"]))
            c.commit()
        return JSON({"discarded": True}) if ok else JSON({"error": "no such draft"}, status_code=404)

    # ---- Teams, a place of its own: the teams and channels, the chats by kind, the fast lane, and posting.
    def teams_places(request: Request):
        with conn() as c:
            chats = {r["k"]: r["n"] for r in c.execute(
                "select headers->>'x-teams-chat-type' as k, count(distinct thread_id) as n from message"
                " where medium = 'teams_chat' group by 1").fetchall()}
            rows = c.execute(
                "select headers->>'x-teams-team' as team, headers->>'x-teams-channel' as channel,"
                " count(distinct thread_id) as threads, count(*) as n, max(received_at) as last_at from message"
                " where medium = 'teams_channel' group by 1, 2 order by 1, max(received_at) desc").fetchall()
        teams: dict[str, dict] = {}
        for r in rows:
            tm = teams.setdefault(r["team"] or "Team", {"team": r["team"] or "Team", "channels": [], "n": 0, "last_at": None})
            tm["channels"].append({"channel": r["channel"] or "Channel", "threads": r["threads"], "n": r["n"], "last_at": r["last_at"]})
            tm["n"] += r["n"]
            tm["last_at"] = max(x for x in (tm["last_at"], r["last_at"]) if x) if r["last_at"] else tm["last_at"]
        return JSON({"chats": chats, "teams": sorted(teams.values(), key=lambda x: str(x["last_at"] or ""), reverse=True)})

    def teams_refresh_state(request: Request):
        # What the page needs to know: when the fast lane last looked (the background service, or a run this
        # server started), and the newest Teams message, so the page redraws only when something arrived.
        with conn() as c:
            cur = c.execute("select state from sync_cursor where account_id = 'teams' and scope = 'fast'").fetchone()
            latest = c.execute("select max(id) as id from message where account_id = 'teams'").fetchone()["id"]
        st = (cur or {}).get("state") or {}
        return JSON({**teams_fast.state(), "checked_at": st.get("checked_at"), "latest_id": latest})

    async def teams_refresh(request: Request):
        # The fast lane while the Teams place is open: `talos sync teams --recent 20` as its own process,
        # at most one at a time and not more often than every TEAMS_FAST_EVERY seconds (TEAMS_BOOST_EVERY for a
        # while after a post).
        if refused := _refused(request):
            return refused
        return JSON(teams_fast.start())

    async def teams_confirm(request: Request):
        # A one-time token for exactly this Teams target and text, usable at once: the owner's Enter in the conversation's
        # box is the confirmation (the page redeems it straight away). Nothing is sent here.
        if refused := _refused_send(request):
            return refused
        try:
            body = await _body(request)
            with conn() as c:
                return JSON(sender.teams_confirm(c, **_teams_target(body)))
        except sending.SendRefused as exc:
            return JSON({"error": str(exc), "code": exc.code}, status_code=429 if exc.code == "rate_limit" else 400)
        except (objects.ObjectError, ValueError, TypeError) as exc:
            return JSON({"error": str(exc)}, status_code=400)

    async def teams_post_send(request: Request):
        # {token, text, thread_id | team+channel}: posted only when the token is for exactly this target and text.
        if refused := _refused_send(request):
            return refused
        try:
            body = await _body(request)
            target = _teams_target(body)
        except (objects.ObjectError, ValueError, TypeError) as exc:
            return JSON({"error": str(exc)}, status_code=400)

        def run():
            with conn() as c:
                try:
                    res = sender.teams_send(c, body.get("token"), **target)
                except sending.SendRefused as exc:
                    return JSON({"error": str(exc), "code": exc.code}, status_code=429 if exc.code == "rate_limit" else 403)
                except sending.SendFailed as exc:
                    return JSON({"error": f"It was not posted: {exc}", "code": "failed"}, status_code=502)
            # Bring it back into the conversation at once (the recent chats, or the one channel posted in), and
            # look there often for a while: the answer usually comes within seconds.
            post = res.pop("post", None)
            channel = (post["team_id"], post["channel_id"]) if post and post.get("team_id") else None
            teams_fast.boost(channel)
            teams_fast.start(force=True, channel=channel)
            return JSON(res)
        return await run_in_threadpool(run)

    async def draft_confirm(request: Request):
        # The confirmation step: the pane's fields are saved, the exact message is checked and
        # summarised, and a one-time token for it comes back. Nothing is sent here.
        if refused := _refused_send(request):
            return refused
        try:
            body = await _body(request)
            did = int(request.path_params["id"])
            with conn() as c:
                if not compose.get_draft(c, did):
                    return JSON({"error": "no such draft"}, status_code=404)
                compose.save_draft(c, did, {k: v for k, v in body.items() if k != "token"})
                c.commit()
                res = sender.confirm(c, did)
                return JSON(res)
        except sending.SendRefused as exc:
            return JSON({"error": str(exc), "code": exc.code}, status_code=429 if exc.code == "rate_limit" else 403)
        except (compose.ComposeError, objects.ObjectError) as exc:
            return JSON({"error": str(exc)}, status_code=400)

    async def draft_send(request: Request):
        # {token, ...the pane's fields}: the fields are saved first, so the message sent is the one
        # the request names; the token must confirm exactly that message.
        if refused := _refused_send(request):
            return refused
        try:
            body = await _body(request)
        except objects.ObjectError as exc:
            return JSON({"error": str(exc)}, status_code=400)
        did = int(request.path_params["id"])

        def run():
            with conn() as c:
                if not compose.get_draft(c, did):
                    return JSON({"error": "no such draft"}, status_code=404)
                try:
                    compose.save_draft(c, did, {k: v for k, v in body.items() if k != "token"})
                    c.commit()
                    return JSON(sender.send(c, did, body.get("token")))
                except sending.SendRefused as exc:
                    return JSON({"error": str(exc), "code": exc.code},
                                status_code=429 if exc.code == "rate_limit" else 403)
                except sending.SendFailed as exc:
                    return JSON({"error": f"The mail was not sent: {exc}", "code": "failed"}, status_code=502)
                except compose.ComposeError as exc:
                    return JSON({"error": str(exc)}, status_code=400)
        return await run_in_threadpool(run)

    # ---- discovery review (talos.discovery): the drafted systems, candidates and the owner's setup;
    # their decision on each, and the binders accepting makes. Writes Talos's own tables only.
    def discovery_items(request: Request):
        with conn() as c:
            return JSON(discovery.items(c))

    async def discovery_decide(request: Request):
        # {ids: [...], decision: accept | reject | null, correction?: {name, kind, description, status, note},
        #  keep_retired?: bool (reject: keep as a retired system binder), mark_retired?: bool}
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
            ids = body.get("ids")
            if not isinstance(ids, list):
                raise discovery.DiscoveryError("ids is a list of item ids")
            with conn() as c:
                rows = discovery.decide(c, ids, body.get("decision"), correction=body.get("correction"),
                                        keep_retired=bool(body.get("keep_retired")),
                                        mark_retired=bool(body.get("mark_retired")))
                return JSON({"items": rows})
        except (discovery.DiscoveryError, objects.ObjectError, ValueError, TypeError) as exc:
            return JSON({"error": str(exc)}, status_code=400)

    async def discovery_bulk(request: Request):
        # {action: accept_active | reject_retired}: undecided items only.
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
            with conn() as c:
                return JSON(discovery.bulk(c, body.get("action") or ""))
        except (discovery.DiscoveryError, objects.ObjectError) as exc:
            return JSON({"error": str(exc)}, status_code=400)

    def events(request: Request):
        with conn() as c:
            return JSON(c.execute(
                "select e.id, e.kind, e.status, e.system, e.occurred_at, e.message_id, m.subject from event e"
                " join message m on m.id = e.message_id order by e.occurred_at desc nulls last limit 200").fetchall())

    # ---- unlocking the labelling (talos.unlock): the low-hanging fruit and the improvement jobs
    # (Tune › Jobs & fruit), read from insight_cache; a stale answer is computed again behind the page.
    # Nothing here calls a model or runs a job: the jobs are commands for the owner to run.
    def unlock_fruit(request: Request):
        with conn() as c:
            return JSON(unlock.fruit(c, dsn=settings.dsn, refresh=bool(_bool(request.query_params.get("refresh")))))

    async def unlock_set_create(request: Request):
        # {top, claude}: a small answer key of the top groups; claude=true also exports it for Claude.
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
            with conn() as c:
                res = unlock.label_set(c, top=int(body.get("top") or unlock.TOP))
                if body.get("claude"):
                    res["export"] = unlock.export_for_claude(c, res["set_id"], settings.exports)
                c.commit()
                return JSON(res, status_code=201)
        except (unlock.UnlockError, gold.GoldError, objects.ObjectError, TypeError, ValueError) as exc:
            return JSON({"error": str(exc)}, status_code=400)

    def unlock_set_export(request: Request):
        if refused := _refused(request):
            return refused
        with conn() as c:
            try:
                res = unlock.export_for_claude(c, int(request.path_params["id"]), settings.exports)
                c.commit()
                return JSON(res)
            except (unlock.UnlockError, gold.GoldError) as exc:
                return JSON({"error": str(exc)}, status_code=400)

    async def unlock_set_apply(request: Request):
        # {dry_run (default true), labeller}: the owner's (or Claude's) answers given to the groups.
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
        except objects.ObjectError as exc:
            return JSON({"error": str(exc)}, status_code=400)
        with conn() as c:
            try:
                res = unlock.apply(c, int(request.path_params["id"]), labeller=body.get("labeller") or None,
                                   dry_run=body.get("dry_run", True) is not False)
                c.commit()
                return JSON(res)
            except (unlock.UnlockError, gold.GoldError) as exc:
                return JSON({"error": str(exc)}, status_code=409)

    def unlock_jobs(request: Request):
        p = request.query_params
        with conn() as c:
            return JSON(unlock.jobs(c, dsn=settings.dsn, refresh=bool(_bool(p.get("refresh"))),
                                    hidden=bool(_bool(p.get("hidden")))))

    async def unlock_job_state(request: Request):
        # {key, state: done | dismissed | null}
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
            with conn() as c:
                res = unlock.mark_job(c, body.get("key"), body.get("state") or None, note=body.get("note"))
                c.commit()
                return JSON(res)
        except (unlock.UnlockError, objects.ObjectError) as exc:
            return JSON({"error": str(exc)}, status_code=400)

    # ---- the Studio (talos.studio): cards of mail Jev is unsure of, the owner's quick decisions, the lifts
    def studio_next(request: Request):
        p = request.query_params
        try:
            with conn() as c:
                return JSON(studio.feed(c, n=min(8, max(1, int(p.get("n") or 3))), exclude=p.getlist("exclude"),
                                        position=max(0, int(p.get("position") or 0)), dsn=settings.dsn))
        except (studio.StudioError, ValueError) as exc:
            return JSON({"error": str(exc)}, status_code=400)

    def studio_stats(request: Request):
        try:
            with conn() as c:
                return JSON({**studio.stats(c, since=studio.parse_since(request.query_params.get("since"))),
                             "calibration": studio.calibration(c), "levels": sureness.levels(),
                             "undecided": studio.undecided(c)})
        except studio.StudioError as exc:
            return JSON({"error": str(exc)}, status_code=400)

    async def studio_write(request: Request):
        # {action: decide | skip | undo | undo_lift, key, lines, scope, rep_id, card, decision, lift}
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
            act = body.get("action")
            with conn() as c:
                if act == "decide":
                    res = studio.decide(c, body.get("key"), body.get("lines") or [], scope=body.get("scope") or "group",
                                        rep_id=body.get("rep_id"), card_kind=body.get("card") or "lever")
                elif act == "skip":
                    res = studio.skip(c, body.get("key"), rep_id=body.get("rep_id"), card_kind=body.get("card") or "lever")
                elif act == "undo":
                    res = studio.undo(c, body.get("decision"))
                elif act == "undo_lift":
                    res = studio.undo_lift(c, int(body.get("lift") or 0))
                else:
                    raise studio.StudioError("action is decide, skip, undo or undo_lift")
                c.commit()
                # the structure plan follows the decision (or its undo), after it is safely committed
                if act in ("decide", "undo") and res.get("decision"):
                    studio.replan(c, res["decision"])
                    c.commit()
                return JSON(res)
        except (studio.StudioError, ValueError, TypeError) as exc:
            return JSON({"error": str(exc)}, status_code=400)

    # ---- machine-mail clusters (talos.clusters): read from insight_cache; cleanup makes planned
    # changesets only (Gmail), and is refused for Microsoft 365, which is planned only (docs/writeback-test-plan.md).
    def clusters_state(request: Request):
        with conn() as c:
            return JSON(clusters.state(c, dsn=settings.dsn, refresh=bool(_bool(request.query_params.get("refresh")))))

    def clusters_examples(request: Request):
        p = request.query_params
        with conn() as c:
            try:
                return JSON(clusters.examples(c, p.get("key") or "", limit=_int(request, "limit", 25, 100),
                                              offset=max(0, int(p.get("offset") or 0))))
            except (clusters.ClusterError, ValueError) as exc:
                return JSON({"error": str(exc)}, status_code=400)

    async def clusters_cleanup(request: Request):
        # {key, action: archive | trash}: planned changesets, never committed.
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
        except objects.ObjectError as exc:
            return JSON({"error": str(exc)}, status_code=400)
        with conn() as c:
            try:
                res = clusters.prepare_cleanup(c, body.get("key") or "", body.get("action") or "")
                c.commit()
                return JSON(res, status_code=201)
            except clusters.Refused as exc:
                return JSON({"error": str(exc), "would": exc.would, "refused": True}, status_code=409)
            except (clusters.ClusterError, clusters.cs.ChangesetError) as exc:
                return JSON({"error": str(exc)}, status_code=400)

    # ---- Argus (talos.argus): check-ins from scripts, its status, and the page's pause and resume.
    # A check-in cannot carry X-Talos (curl, not the page), so it carries the check-in token; the
    # Keychain is read in the thread pool, never on the event loop, and only on the first check-in.
    from talos import secrets
    argus_conf = argus.load_settings(settings.home)
    timers_on = bool(argus_conf.get("enabled")) if argus_timers is None else argus_timers
    notifier = argus_notifier if argus_notifier is not None else \
        (argus.Notifier() if timers_on and argus_conf.get("notify", True) else None)
    token_check = argus.TokenCheck(argus_token or (lambda: secrets.get_optional(argus.TOKEN_KEY)))
    try:
        beat_minutes = max(1, int(argus_conf.get("beat_every_minutes", argus.BEAT_EVERY_MINUTES)))
    except (TypeError, ValueError):
        beat_minutes = argus.BEAT_EVERY_MINUTES
    monitor = argus.Monitor(settings.dsn, notifier=notifier, beat_url=lambda: secrets.get_optional(argus.BEAT_KEY),
                            beat_every=beat_minutes * 60)
    beat_seen = {"at": 0.0, "value": False}

    def _beat_configured() -> bool:
        # Whether the heartbeat URL is in the Keychain, without reading it (no access dialog); asked
        # at most once a minute.
        if beat_configured is not None:
            return beat_configured()
        if time.monotonic() - beat_seen["at"] > 60:
            beat_seen.update(at=time.monotonic(), value=secrets.exists(argus.BEAT_KEY))
        return beat_seen["value"]

    def _argus_store(slug: str, ok: bool, within, summary) -> dict:
        with conn() as c:
            row = argus.record(c, slug, ok=ok, expected_next_within=within, summary=summary)
            argus.evaluate(c, notifier=notifier, slugs=[slug])
            st = c.execute("select status from argus_service where slug = %s", (slug,)).fetchone()["status"]
            c.commit()
        return {"slug": slug, "ok": ok, "status": st, "expected_next_at": row["expected_next_at"]}

    async def _argus_in(request: Request, fail: bool):
        if not await run_in_threadpool(token_check.configured):
            return JSON({"error": "no check-in token is set up; see: talos argus token"}, status_code=503)
        if not await run_in_threadpool(token_check, _bearer(request) or request.query_params.get("token")):
            return JSON({"error": "a check-in needs the Argus token (Authorization: Bearer …)"}, status_code=401,
                        headers={"WWW-Authenticate": "Bearer"})
        try:
            p = await _argus_params(request)
            slug = argus.check_slug(request.path_params["slug"])
            ok = False if fail else _truth(p.get("ok", True))
            summary = (p.get("reason") or p.get("summary")) if fail else p.get("summary") or p.get("reason")
            return JSON(await run_in_threadpool(_argus_store, slug, ok, p.get("expected_next_within"), summary))
        except argus.UnknownService as exc:
            return JSON({"error": str(exc)}, status_code=404)
        except argus.ArgusError as exc:
            return JSON({"error": str(exc)}, status_code=413 if "4 kB" in str(exc) else 400)

    async def argus_checkin(request: Request):
        return await _argus_in(request, fail=False)

    async def argus_fail(request: Request):
        return await _argus_in(request, fail=True)

    def argus_status(request: Request):
        with conn() as c:
            res = argus.overview(c, beat_configured=_beat_configured())
        res["monitor"] = {"timers": timers_on, "notifications": notifier is not None,
                          "last_tick_at": monitor.last_tick_at, "sweep_every": argus.SWEEP_EVERY,
                          "beat_every": monitor.beat_every}
        return JSON(res)

    def argus_action(request: Request):
        if refused := _refused(request):
            return refused
        slug, action = request.path_params["slug"], request.path_params["action"]
        try:
            with conn() as c:
                if action in ("pause", "resume"):
                    argus.set_paused(c, slug, action == "pause")
                elif action == "ack":
                    argus.acknowledge(c, slug, monitor.tools)
                else:
                    return JSON({"error": "the action is pause, resume or ack"}, status_code=404)
                argus.evaluate(c, notifier=notifier, slugs=[slug])
                return JSON(argus.overview(c, beat_configured=_beat_configured()))
        except argus.UnknownService as exc:
            return JSON({"error": str(exc)}, status_code=404)
        except argus.ArgusError as exc:
            return JSON({"error": str(exc)}, status_code=400)

    # ---- the work space and Discover (talos.space, talos.discover)
    def space_state(request: Request):
        with conn() as c:
            return JSON(space.overview(c, dsn=settings.dsn))

    def space_binder(request: Request):
        oid = int(request.path_params["id"])
        with conn() as c:
            try:
                return JSON({"found": space.found(c, oid), "neighbours": space.neighbours(c, oid, dsn=settings.dsn),
                             "watchers": space.watchers(c, object_id=oid)})
            except space.SpaceError as exc:
                return JSON({"error": str(exc)}, status_code=404)

    async def _space_write(request: Request, fn):
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
            with conn() as c:
                out = fn(c, body)
                c.commit()
                return JSON(out)
        except (space.SpaceError, discover.DiscoverError, objects.ObjectError) as exc:
            return JSON({"error": str(exc)}, status_code=400)

    async def space_terms(request: Request):
        oid = int(request.path_params["id"])
        return await _space_write(request, lambda c, b: {"terms": space.set_terms(c, oid, b.get("terms") or [])})

    async def space_found(request: Request):
        # "Found in your mail": add a thread to the binder, or keep it out ("not this").
        oid = int(request.path_params["id"])

        def act(c, b):
            tid, action = int(b.get("thread_id") or 0), b.get("action")
            if action not in ("add", "exclude") or not tid:
                raise space.SpaceError("action is add or exclude, with a thread_id")
            return {"changed": (objects.add if action == "add" else objects.exclude)(c, oid, [tid])}
        return await _space_write(request, act)

    async def watcher_add(request: Request):
        return await _space_write(request, lambda c, b: {"id": space.add_watcher(
            c, b.get("name", ""), b.get("query", ""), object_id=int(b["object_id"]) if b.get("object_id") else None)})

    async def watcher_update(request: Request):
        wid = int(request.path_params["id"])

        def act(c, b):
            space.update_watcher(c, wid, seen=bool(b.get("seen")), paused=b.get("paused"), name=b.get("name"),
                                 object_id=(int(b["object_id"]) if b["object_id"] else None) if "object_id" in b else False)
            return {"ok": True}
        return await _space_write(request, act)

    async def watcher_remove(request: Request):
        wid = int(request.path_params["id"])
        return await _space_write(request, lambda c, b: space.remove_watcher(c, wid) or {"ok": True})

    async def watcher_take_in(request: Request):
        wid = int(request.path_params["id"])
        return await _space_write(request, lambda c, b: {"added": space.take_in(c, wid)})

    def discover_state(request: Request):
        with conn() as c:
            return JSON(discover.state(c, dsn=settings.dsn, refresh=bool(_bool(request.query_params.get("refresh")))))

    def aggregation_list(request: Request):
        with conn() as c:
            return JSON(discover.aggregations_counted(c, dsn=settings.dsn, refresh=bool(_bool(request.query_params.get("refresh")))))

    async def aggregation_add(request: Request):
        return await _space_write(request, lambda c, b: {"id": discover.add(
            c, b.get("name", ""), b.get("query", ""), b.get("group_by", ""), description=b.get("description", ""))})

    async def aggregation_remove(request: Request):
        aid = int(request.path_params["id"])
        return await _space_write(request, lambda c, b: discover.remove(c, aid) or {"ok": True})

    def sync_state(request: Request):
        with conn() as c:
            accts = syncnow.mail_accounts(c)
        return JSON({**sync_now.state(), "available": [a["id"] for a in accts]})

    async def sync_start(request: Request):
        # Sync now: the mail accounts (all, or the ones asked for), never Teams.
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
        except objects.ObjectError as exc:
            return JSON({"error": str(exc)}, status_code=400)
        with conn() as c:
            available = [a["id"] for a in syncnow.mail_accounts(c)]
        asked = body.get("accounts") or available
        if not isinstance(asked, list) or any(a not in available for a in asked):
            return JSON({"error": "Sync now takes the enabled mail accounts: " + ", ".join(available)}, status_code=400)
        return JSON({**sync_now.start(asked), "available": available})

    # ---- the door (web.gate, talos.webauth, docs/security.md)
    def login_page(request: Request):
        # Fingerprinted like the app, so a changed icon reaches the sign-in page too.
        return Response(_fingerprinted("login.html"), media_type="text/html", headers={"Cache-Control": "no-store"})

    def favicon(request: Request):
        # Safari (and other browsers) ask for /favicon.ico on their own, before or without reading the page's
        # links. It is the tab icon and holds no mail, so it is one of the gate's public paths.
        return FileResponse(STATIC / "icons" / "beacon-tab.ico", media_type="image/x-icon",
                            headers={"Cache-Control": "no-cache"})

    def release_notes(request: Request):
        # CHANGELOG.md as data (talos.releases): this version and every release, newest first.
        return JSON(releases.notes())

    def owner_info(request: Request):
        # Who the owner is to the page (talos.personal): the name their decisions are stored under, and
        # each account's name, letter and colour (accounts.json's ui settings, else derived), in order.
        from talos import accounts as owned   # this module names many locals "accounts"
        return JSON({"id": personal.OWNER_ID, "accounts": owned.ui()})

    def manual_sections(request: Request):
        # docs/manual.md as data (talos.manual): the sections the ⓘ buttons open.
        return JSON(manual.sections())

    def auth_status(request: Request):
        with conn() as c:
            until = webauth.locked_until(c)
        return JSON({"set_up": webauth.is_set_up(store), "locked_until": until,
                     "signed_in": bool(request.scope.get("state", {}).get("session"))})

    # Signing in (the door, docs/security.md): the owner's password and an authenticator code, checked by
    # webauth in a worker thread. Wrong attempts are counted; past the limit signing in locks for a
    # while and a macOS notice says so. Every new session is announced too, so a sign-in they did not
    # make is seen at once. The session cookie is HttpOnly, and Secure over the tailnet.
    async def auth_login(request: Request):
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
        except objects.ObjectError as exc:
            return JSON({"error": str(exc)}, status_code=400)
        origin = request.scope["state"]["origin"]
        agent = request.headers.get("user-agent", "")

        def attempt():
            with conn() as c:
                try:
                    return webauth.sign_in(c, store, str(body.get("password") or ""), str(body.get("code") or ""),
                                           origin=origin, user_agent=agent), None
                except webauth.AuthError as exc:
                    return None, (str(exc), webauth.locked_until(c))
        token, err = await run_in_threadpool(attempt)
        if err:
            if door_notify and err[1]:
                door_notify("Talos: signing in locked", f"Too many wrong attempts from {origin}. Locked for "
                                                        f"{int(webauth.LOCK_WINDOW.total_seconds() // 60)} minutes.")
            return JSON({"error": err[0], "locked_until": err[1]}, status_code=429 if err[1] else 401)
        if door_notify:
            door_notify("Talos: signed in", f"A new session from {origin}.")
        resp = JSON({"ok": True})
        resp.headers["set-cookie"] = gate.cookie_header(token, secure=request.scope["state"]["secure"],
                                                        max_age=int(webauth.MAX_AGE.total_seconds()))
        return resp

    def _door_write(request: Request, fn):
        if refused := _refused(request):
            return refused
        with conn() as c:
            return fn(c)

    def auth_logout(request: Request):
        token = gate.token_from({"cookie": request.headers.get("cookie", "")})
        out = _door_write(request, lambda c: webauth.sign_out(c, token))
        if isinstance(out, JSONResponse):
            return out
        resp = JSON({"ok": True})
        resp.headers["set-cookie"] = gate.cookie_header("", secure=request.scope["state"]["secure"], max_age=0)
        return resp

    def auth_signout_all(request: Request):
        me = request.scope["state"]["session"].id
        out = _door_write(request, lambda c: webauth.sign_out(c, None, everywhere=True, keep=me))
        return out if isinstance(out, JSONResponse) else JSON({"ended": out})

    async def auth_step_up(request: Request):
        if refused := _refused(request):
            return refused
        try:
            body = await _body(request)
        except objects.ObjectError as exc:
            return JSON({"error": str(exc)}, status_code=400)
        session = request.scope["state"]["session"]

        def go():
            with conn() as c:
                webauth.step_up(c, store, session, str(body.get("code") or ""), read_budget)
        try:
            await run_in_threadpool(go)
        except webauth.AuthError as exc:
            return JSON({"error": str(exc)}, status_code=401)
        return JSON({"ok": True})

    def auth_me(request: Request):
        session = request.scope["state"].get("session")
        with conn() as c:
            return JSON({"session": session.__dict__ if session else None,
                         "sessions": [{**r, "current": bool(session and r["id"] == session.id), "id": r["id"][:8]}
                                      for r in webauth.sessions(c)],
                         "events": webauth.recent_events(c, 25),
                         "limits": {"idle_hours": webauth.IDLE.total_seconds() / 3600,
                                    "max_days": webauth.MAX_AGE.days, "read_budget": read_budget.budget,
                                    "read_window_minutes": read_budget.window // 60}})

    @contextlib.asynccontextmanager
    async def lifespan(app):
        # The Argus timers run inside talos serve (launchd keeps it alive), when argus.json says so.
        stop, task = asyncio.Event(), None
        if timers_on:
            task = asyncio.create_task(monitor.run(stop))
        try:
            yield
        finally:
            if task:
                stop.set()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(task, 15)

    def index(request: Request):
        # Fingerprint the assets, as Overcast does: a changed file gets a new URL, so a browser
        # can never run yesterday's script against today's API. Every /static/ file the page
        # names (the script, the styles, the icons and the manifest) gets its digest.
        return Response(_fingerprinted(), media_type="text/html", headers={"Cache-Control": "no-store"})

    routes = [
        Route("/", index),
        Route("/login", login_page),
        Route("/favicon.ico", favicon),
        Route("/auth/status", auth_status),
        Route("/auth/login", auth_login, methods=["POST"]),
        Route("/auth/logout", auth_logout, methods=["POST"]),
        Route("/auth/signout-all", auth_signout_all, methods=["POST"]),
        Route("/auth/step-up", auth_step_up, methods=["POST"]),
        Route("/auth/me", auth_me),
        Route("/api/release-notes", release_notes),
        Route("/api/manual", manual_sections),
        Route("/api/owner", owner_info),
        Route("/api/overview", overview),
        Route("/api/space", space_state),
        Route("/api/space/binders/{id:int}", space_binder),
        Route("/api/space/binders/{id:int}/terms", space_terms, methods=["POST"]),
        Route("/api/space/binders/{id:int}/found", space_found, methods=["POST"]),
        Route("/api/watchers", watcher_add, methods=["POST"]),
        Route("/api/watchers/{id:int}", watcher_update, methods=["POST"]),
        Route("/api/watchers/{id:int}/remove", watcher_remove, methods=["POST"]),
        Route("/api/watchers/{id:int}/take-in", watcher_take_in, methods=["POST"]),
        Route("/api/discover", discover_state),
        Route("/api/aggregations", aggregation_list),
        Route("/api/aggregations", aggregation_add, methods=["POST"]),
        Route("/api/aggregations/{id:int}/remove", aggregation_remove, methods=["POST"]),
        Route("/api/sync", sync_state),
        Route("/api/sync", sync_start, methods=["POST"]),
        Route("/api/messages", messages),
        Route("/api/threads", threads),
        Route("/api/threads/{id:int}", thread_detail),
        Route("/api/senders", senders),
        Route("/api/domains", domains),
        Route("/api/facets", facet_values),
        Route("/api/top-senders", top_senders),
        Route("/api/dimensions", dimensions),
        Route("/api/messages/{id:int}", message),
        Route("/api/messages/{id:int}/raw", message_raw),
        Route("/api/messages/{id:int}/html", message_html),
        Route("/api/attachments/{id:int}", attachment),
        Route("/api/changesets", changesets),
        Route("/api/changesets/{id:int}", changeset_detail),
        Route("/api/changesets/{id:int}/dry-run", changeset_dry_run, methods=["POST"]),
        Route("/api/structure", structure_state),
        Route("/api/structure/examples", structure_examples),
        Route("/api/structure/why/{id:int}", structure_why),
        Route("/api/structure/checklist", structure_checklist),
        Route("/api/structure/prepare", structure_prepare, methods=["POST"]),
        Route("/api/rules", rule_list),
        Route("/api/rules", rule_save, methods=["POST"]),
        Route("/api/rules/preview", rule_preview, methods=["POST"]),
        Route("/api/rules/run", rule_run, methods=["POST"]),
        Route("/api/rules/suggestions", rule_suggestions),
        Route("/api/rules/suggestions/add", rule_suggestion_add, methods=["POST"]),
        Route("/api/rules/suggestions/tree", rule_suggestion_tree),
        Route("/api/rules/suggestions/group/preview", rule_group_preview, methods=["POST"]),
        Route("/api/rules/suggestions/group", rule_group_add, methods=["POST"]),
        Route("/api/rules/removed", rule_removed),
        Route("/api/rules/{id}/remove", rule_remove, methods=["POST"]),
        Route("/api/rules/{id}/enabled", rule_enabled, methods=["POST"]),
        Route("/api/objects", object_list),
        Route("/api/objects", object_create, methods=["POST"]),
        Route("/api/objects/{id:int}", object_detail),
        Route("/api/objects/{id:int}", object_update, methods=["POST"]),
        Route("/api/objects/{id:int}/members", object_members, methods=["POST"]),
        Route("/api/objects/{id:int}/activity", object_activity),
        Route("/api/work", work_list),
        Route("/api/work", work_create, methods=["POST"]),
        Route("/api/work/attention", work_attention),
        Route("/api/work/homes", work_homes),
        Route("/api/work/{id:int}", work_detail),
        Route("/api/work/{id:int}", work_update, methods=["POST"]),
        Route("/api/work/{id:int}/messages", work_messages, methods=["POST"]),
        Route("/api/calendar", calendar_state),
        Route("/api/timeline", timeline_state),
        Route("/api/calendar/entries", calendar_entry_create, methods=["POST"]),
        Route("/api/calendar/entries/{id:int}", calendar_entry_update, methods=["POST"]),
        Route("/api/calendar/calendars", calendar_create, methods=["POST"]),
        Route("/api/calendar/calendars/{id:int}", calendar_update, methods=["POST"]),
        Route("/api/calendar/sync", calendar_sync, methods=["POST"]),
        Route("/api/events", events),
        Route("/api/acceptance", acceptance_state),
        Route("/api/acceptance/examples", acceptance_examples),
        Route("/api/acceptance/apply", acceptance_apply, methods=["POST"]),
        Route("/api/gold", gold_sets),
        Route("/api/gold/{id:int}", gold_set),
        Route("/api/gold/{id:int}/items/{pos:int}", gold_item),
        Route("/api/gold/{id:int}/items/{pos:int}/labels", gold_label, methods=["POST"]),
        Route("/api/gold/{id:int}/rounds/{round:int}/reveal", gold_reveal, methods=["POST"]),
        Route("/api/gold/{id:int}/check", gold_check),
        Route("/api/gold/{id:int}/check/summary", gold_check_summary),
        Route("/api/gold/{id:int}/check/{rank:int}", gold_check_item),
        Route("/api/gold/{id:int}/check/{rank:int}/agree", gold_check_agree, methods=["POST"]),
        Route("/api/importance/today", importance_today),
        Route("/api/importance/waiting", importance_waiting),
        Route("/api/importance/check", importance_check),
        Route("/api/messages/{id:int}/importance", importance_mark, methods=["POST"]),
        Route("/api/compose/senders", compose_senders),
        Route("/api/compose/settings", compose_settings, methods=["POST"]),
        Route("/api/compose/suggest", compose_suggest),
        Route("/api/compose/signatures", signature_list),
        Route("/api/compose/signatures", signature_save, methods=["POST"]),
        Route("/api/compose/signatures/{id:int}", signature_save, methods=["POST"]),
        Route("/api/compose/signatures/{id:int}/remove", signature_remove, methods=["POST"]),
        Route("/api/drafts", draft_list),
        Route("/api/drafts", draft_create, methods=["POST"]),
        Route("/api/drafts/{id:int}", draft_detail),
        Route("/api/drafts/{id:int}", draft_save, methods=["POST"]),
        Route("/api/drafts/{id:int}/discard", draft_discard, methods=["POST"]),
        Route("/api/drafts/{id:int}/confirm", draft_confirm, methods=["POST"]),
        Route("/api/drafts/{id:int}/send", draft_send, methods=["POST"]),
        Route("/api/teams/places", teams_places),
        Route("/api/teams/refresh", teams_refresh_state),
        Route("/api/teams/refresh", teams_refresh, methods=["POST"]),
        Route("/api/teams/confirm", teams_confirm, methods=["POST"]),
        Route("/api/teams/send", teams_post_send, methods=["POST"]),
        Route("/argus/checkin/{slug}", argus_checkin, methods=["POST"]),
        Route("/argus/fail/{slug}", argus_fail, methods=["POST"]),
        Route("/argus/status", argus_status),
        Route("/api/argus/{slug}/{action}", argus_action, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
        Route("/api/unlock/fruit", unlock_fruit),
        Route("/api/unlock/sets", unlock_set_create, methods=["POST"]),
        Route("/api/unlock/sets/{id:int}/export", unlock_set_export, methods=["POST"]),
        Route("/api/unlock/sets/{id:int}/apply", unlock_set_apply, methods=["POST"]),
        Route("/api/unlock/jobs", unlock_jobs),
        Route("/api/studio/next", studio_next),
        Route("/api/studio/stats", studio_stats),
        Route("/api/studio", studio_write, methods=["POST"]),
        Route("/api/unlock/jobs/state", unlock_job_state, methods=["POST"]),
        Route("/api/clusters", clusters_state),
        Route("/api/clusters/examples", clusters_examples),
        Route("/api/clusters/cleanup", clusters_cleanup, methods=["POST"]),
        Route("/api/discovery", discovery_items),
        Route("/api/discovery/decide", discovery_decide, methods=["POST"]),
        Route("/api/discovery/bulk", discovery_bulk, methods=["POST"]),
    ]
    web = settings.web
    tailnet_hosts = list(web.get("tailnet_hosts") or [])
    return Starlette(routes=routes, lifespan=lifespan, middleware=[
        Middleware(TrustedHostMiddleware, allowed_hosts=(allowed_hosts or ALLOWED_HOSTS) + tailnet_hosts),
        Middleware(TailnetIdentity, hosts=tailnet_hosts, users=list(web.get("tailnet_users") or [])),
        Middleware(gate.Gate, conn=conn, reads=read_budget, notify=door_notify, tailnet_hosts=tailnet_hosts,
                   argus_token=token_check, csp=gate.page_csp(gate.inline_script_hashes((STATIC / "index.html").read_text())),
                   enforce=enforce)])
