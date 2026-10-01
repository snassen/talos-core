"""Microsoft Teams over Microsoft Graph: chats and channel messages, read-only.

Uses the work account's sign-in (Chat.Read, ChannelMessage.Read.All) through the same GET-only
GraphClient as mail; only GET requests are ever sent. The account row is 'teams', so
its runs, cursors and lock are separate from the work account's mail.

The fast lane (recent=N, talos sync teams --recent N): GET /me/chats ordered by the newest message,
top N, and those chats' new messages only; no channels. Near-instant chats without a full pass.

Chats (1:1, group, meeting):
  GET /me/chats?$expand=lastMessagePreview      every chat, with its newest message
  GET /chats/{id}/members                       user id → e-mail, once per chat per run
  GET /chats/{id}/messages?$top=50              backfill, newest first, page by page
  GET /chats/{id}/messages?$orderby=lastModifiedDateTime desc
      &$filter=lastModifiedDateTime gt {since}  afterwards: only new or edited messages
  A chat whose newest message is older than its cursor is not asked again until
  RECHECK has passed (edits and deletions of old messages are caught then).
  Cursor 'chat:<id>': since (newest lastModifiedDateTime covered), next_link while a
  pass is under way, pending_since (the newest seen in that pass), checked_at.

Channels (teams the owner is a member of):
  GET /me/joinedTeams, /teams/{id}/channels
  GET /teams/{t}/channels/{c}/messages?$top=50&$expand=replies     backfill
  GET /teams/{t}/channels/{c}/messages/{m}/replies                 when replies were cut off
  GET /teams/{t}/channels/{c}/messages/delta?$filter=lastModifiedDateTime gt {start}
      then the deltaLink: new or edited root messages; their replies are re-read.
  Every RECHECK, the replies of threads active in the last SWEEP_DAYS are re-read too,
  since a new reply to an old post may not surface in the delta.
  Cursor 'channel:<team>:<channel>': next_link / backfilled / started_at / delta_link.

A 403 or 404 on one chat or channel (left, deleted, no access) is recorded in its
cursor and skipped for a day; it never stops the run. Each page is committed together
with the cursor that covers it.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from talos.sources.base import SyncContext, SyncStats
from talos.sources.graph import GraphClient, GraphError
from talos.teams_ingest import Conversation, TeamsIngestor, dt

log = logging.getLogger("talos.sync.teams")

PAGE = 50                       # Graph's maximum $top for Teams messages
RECHECK = timedelta(hours=6)    # how often a quiet chat or channel is asked about edits anyway
ERROR_BACKOFF = timedelta(hours=24)  # after a 403/404
FAULT_BACKOFF = timedelta(hours=1)   # after any other failure, so one broken chat cannot starve the rest
SWEEP_DAYS = 14
SWEEP_THREADS = 50
SKIPPABLE = {403, 404}
CHATS_FOLDER = "Teams/Chats"


def status_of(exc: Exception) -> int | None:
    m = re.search(r"-> (\d{3})", str(exc))
    return int(m.group(1)) if m else None


def _ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


class _Budget:
    def __init__(self, limit: int | None):
        self.left = limit

    def spend(self, n: int) -> None:
        if self.left is not None:
            self.left -= n

    @property
    def done(self) -> bool:
        return self.left is not None and self.left <= 0


class TeamsSource:
    def __init__(self, client: GraphClient, *, channels: bool = True, extra_channels: list[dict] | None = None,
                 clock=lambda: datetime.now(timezone.utc), recent: int | None = None, only_channel: dict | None = None):
        self.client = client
        self.channels = channels
        # The fast lane (talos sync teams --recent N): only the N chats with the newest messages,
        # one request to find them, and no channels. The full pass stays the 5-minute job's.
        self.recent = recent
        # One channel only ({"team": {id, displayName}, "channel": {id, displayName}}), right after the owner
        # posted in it, so the post comes back in seconds rather than at the next 5-minute run.
        self.only_channel = only_channel
        self.extra_channels = extra_channels or []
        self.clock = clock

    # ------------------------------------------------------------------ plumbing

    def get(self, url: str) -> dict:
        return self.client.get(url, page_size=PAGE)

    def all_pages(self, url: str) -> list[dict]:
        out = []
        while url:
            page = self.get(url)
            out.extend(page.get("value", []))
            url = page.get("@odata.nextLink")
        return out

    def sync(self, ctx: SyncContext) -> SyncStats:
        stats = SyncStats()
        tin = TeamsIngestor(ctx.ingestor, ctx.account_id)
        self._me(ctx, tin)
        budget = _Budget(ctx.limit)
        if self.only_channel:
            team, ch = self.only_channel["team"], self.only_channel["channel"]
            self._guarded(ctx, tin, stats, f"channel:{team['id']}:{ch['id']}", f"channel {ch.get('displayName')}",
                          lambda state: self._channel(ctx, tin, stats, budget, team, ch, state))
            return stats
        self._chats(ctx, tin, stats, budget)
        if self.channels and not self.recent and not budget.done:
            self._all_channels(ctx, tin, stats, budget)
        if budget.done:
            stats.notes.append("limit reached; run again to continue")
        return stats

    def _me(self, ctx: SyncContext, tin: TeamsIngestor) -> None:
        me = ctx.cursors.get("me")
        if not me:
            got = self.get("/me?$select=id,mail,userPrincipalName")
            me = {"id": got.get("id"), "mail": got.get("mail"), "upn": got.get("userPrincipalName")}
            with ctx.conn.transaction():
                ctx.cursors.put("me", me)
        tin.set_me(me.get("id"))

    def _count(self, stats: SyncStats, budget: _Budget, res: str) -> None:
        if res == "skipped":
            return
        stats.seen += 1
        if res == "added":
            stats.added += 1
            budget.spend(1)
        elif res == "updated":
            stats.updated += 1
        elif res == "failed":
            stats.failed += 1

    def _guarded(self, ctx, tin, stats, scope: str, label: str, fn) -> None:
        """Run one chat or channel.

        A 403/404 is written to its cursor and the run goes on; that chat is left alone for a
        day. Any other failure is written down too and then fails the run, as for mail; that
        chat then waits an hour, so the next runs reach the chats after it.
        """
        state = ctx.cursors.get(scope) or {}
        if state.get("error_at"):
            wait = ERROR_BACKOFF if state.get("error_status") in SKIPPABLE else FAULT_BACKOFF
            if self.clock() - dt(state["error_at"]) < wait:
                return
        try:
            fn(state)
        except Exception as exc:
            tin.reset_caches()
            status = status_of(exc) if isinstance(exc, GraphError) else None
            with ctx.conn.transaction():
                fresh = ctx.cursors.get(scope) or {}
                ctx.cursors.put(scope, {**fresh, "error": f"{type(exc).__name__}: {exc}"[:300],
                                        "error_status": status, "error_at": self.clock().isoformat()})
            if status not in SKIPPABLE:
                raise
            log.warning("teams skipped %s (%s): %s", scope, label, exc)
            stats.notes.append(f"skipped {label}: HTTP {status}")

    # ------------------------------------------------------------------ chats

    def _chats(self, ctx, tin, stats, budget) -> None:
        if self.recent:
            chats = self.get(f"/me/chats?$expand=lastMessagePreview&$orderby=lastMessagePreview/createdDateTime%20desc"
                             f"&$top={max(1, min(self.recent, PAGE))}").get("value", [])
            for chat in chats:
                label = f"chat {chat.get('topic') or chat['id']}"
                self._guarded(ctx, tin, stats, f"chat:{chat['id']}", label,
                              lambda state, chat=chat: self._chat(ctx, tin, stats, budget, chat, state))
            return
        try:
            chats = self.all_pages(f"/me/chats?$expand=lastMessagePreview&$top={PAGE}")
        except GraphError as exc:
            if status_of(exc) != 400:
                raise
            chats = self.all_pages(f"/me/chats?$top={PAGE}")  # expand refused: every chat is then asked
        for chat in chats:
            if budget.done:
                return
            label = f"chat {chat.get('topic') or chat['id']}"
            self._guarded(ctx, tin, stats, f"chat:{chat['id']}", label,
                          lambda state, chat=chat: self._chat(ctx, tin, stats, budget, chat, state))

    def _chat(self, ctx, tin, stats, budget, chat: dict, state: dict) -> None:
        cid = chat["id"]
        scope = f"chat:{cid}"
        now = self.clock()
        since = state.get("since")
        if state.get("next_link"):
            url = state["next_link"]
        elif state.get("backfilled"):
            # lastMessagePreview is present (possibly null, for an empty chat) only when the listing
            # expanded it; without it every chat is asked every run.
            preview = dt((chat.get("lastMessagePreview") or {}).get("createdDateTime"))
            quiet = "lastMessagePreview" in chat and (preview is None or bool(since and preview <= dt(since)))
            checked = dt(state.get("checked_at"))
            if quiet and checked and now - checked < RECHECK:
                return  # nothing new said, and edits were looked for recently
            if since:
                upper = _ts(now + timedelta(days=1))
                flt = quote(f"lastModifiedDateTime gt {since} and lastModifiedDateTime lt {upper}")
                url = f"/chats/{cid}/messages?$top={PAGE}&$orderby=lastModifiedDateTime%20desc&$filter={flt}"
            else:
                url = f"/chats/{cid}/messages?$top={PAGE}"
        else:
            url = f"/chats/{cid}/messages?$top={PAGE}"
        conv = None
        pending = state.get("pending_since") or since
        while url:
            page = self.get(url)
            items = page.get("value", [])
            nxt = page.get("@odata.nextLink")
            try:
                with ctx.conn.transaction():
                    if items and conv is None:
                        conv = self._chat_conversation(tin, chat)
                    for msg in items:
                        self._count(stats, budget, tin.ingest(conv, msg))
                        lm = msg.get("lastModifiedDateTime") or msg.get("createdDateTime")
                        if lm and (not pending or dt(lm) > dt(pending)):
                            pending = _ts(dt(lm))
                    new = {k: v for k, v in state.items() if k not in ("error", "error_at", "error_status")}
                    new.update(topic=chat.get("topic"), chat_type=chat.get("chatType"))
                    if nxt:
                        new.update(next_link=nxt, pending_since=pending)
                    else:
                        new.update(next_link=None, pending_since=None, since=pending, backfilled=True,
                                   checked_at=now.isoformat())
                    ctx.cursors.put(scope, new)
                    state = new
            except Exception:
                tin.reset_caches()
                raise
            url = nxt
            if budget.done and url:
                return

    def _chat_conversation(self, tin: TeamsIngestor, chat: dict) -> Conversation:
        members = chat.get("members")
        if members is None:
            try:
                members = self.all_pages(f"/chats/{chat['id']}/members")
            except GraphError as exc:
                if status_of(exc) not in SKIPPABLE:
                    raise
                members = []  # the messages are still worth having; senders known from other chats resolve
        tin.learn_members(members)
        ids = [m["userId"] for m in members if m.get("userId")]
        others = [m.get("displayName") for m in members if m.get("userId") not in tin.my_ids and m.get("displayName")]
        fallback = ", ".join(others[:4]) + (f" +{len(others) - 4}" if len(others) > 4 else "") if others else "Teams chat"
        return Conversation(
            kind="chat", key_prefix=f"chat:{chat['id']}", thread_key=f"chat:{chat['id']}", folder=CHATS_FOLDER,
            subject=chat.get("topic") or None, fallback_subject=fallback, member_ids=ids,
            meta={"chat_id": chat["id"], "chat_type": chat.get("chatType")})

    # ------------------------------------------------------------------ channels

    def _all_channels(self, ctx, tin, stats, budget) -> None:
        found: list[tuple[dict, dict]] = []
        try:
            for team in self.all_pages("/me/joinedTeams"):
                try:
                    for ch in self.all_pages(f"/teams/{team['id']}/channels"):
                        found.append((team, ch))
                except GraphError as exc:
                    if status_of(exc) not in SKIPPABLE:
                        raise
                    stats.notes.append(f"skipped team {team.get('displayName') or team['id']}: HTTP {status_of(exc)}")
        except GraphError as exc:
            if status_of(exc) not in SKIPPABLE:
                raise
            # Listing teams and channels needs Team.ReadBasic.All / Channel.ReadBasic.All, which the
            # work account's sign-in may not have. Channels named in account settings are still read.
            stats.notes.append(f"could not list joined teams (HTTP {status_of(exc)}); channels not listed")
        known = {(t["id"], c["id"]) for t, c in found}
        for extra in self.extra_channels:
            if (extra["team_id"], extra["channel_id"]) not in known:
                found.append(({"id": extra["team_id"], "displayName": extra.get("team_name")},
                              {"id": extra["channel_id"], "displayName": extra.get("channel_name")}))
        for team, ch in found:
            if budget.done:
                return
            scope = f"channel:{team['id']}:{ch['id']}"
            label = f"channel {team.get('displayName') or team['id']}/{ch.get('displayName') or ch['id']}"
            self._guarded(ctx, tin, stats, scope, label,
                          lambda state, team=team, ch=ch: self._channel(ctx, tin, stats, budget, team, ch, state))

    def _channel(self, ctx, tin, stats, budget, team: dict, ch: dict, state: dict) -> None:
        tid, cid = team["id"], ch["id"]
        scope = f"channel:{tid}:{cid}"
        base = f"/teams/{tid}/channels/{cid}/messages"
        now = self.clock()
        tname, cname = team.get("displayName") or tid, ch.get("displayName") or cid
        where = {"folder": f"Teams/{tname}/{cname}", "label": f"{tname} › {cname}",
                 "meta": {"team_id": tid, "channel_id": cid, "team": tname, "channel": cname}}
        if not state.get("backfilled"):
            state = {**state, "started_at": state.get("started_at") or _ts(now - timedelta(minutes=5))}
            url = state.get("next_link") or f"{base}?$top={PAGE}&$expand=replies"
            done = self._channel_pages(ctx, tin, stats, budget, scope, state, url, where, base, delta=False)
            if not done:
                return
            state = ctx.cursors.get(scope)
        url = state.get("delta_next") or state.get("delta_link")
        try:
            first = url or f"{base}/delta?$top={PAGE}&$filter=" + quote(f"lastModifiedDateTime gt {state['started_at']}")
            finished = self._channel_pages(ctx, tin, stats, budget, scope, state, first, where, base, delta=True)
        except GraphError as exc:
            if url or status_of(exc) != 400:
                raise
            # The filter was refused: start the delta from scratch; what was backfilled is a no-op.
            finished = self._channel_pages(ctx, tin, stats, budget, scope, state, f"{base}/delta", where, base,
                                           delta=True)
        if not finished:
            return
        state = ctx.cursors.get(scope)
        checked = dt(state.get("swept_at"))
        if not checked or now - checked >= RECHECK:
            self._sweep(ctx, tin, stats, budget, scope, state, where, base, tid, cid)

    def _channel_pages(self, ctx, tin, stats, budget, scope, state, url, where, base, *, delta: bool) -> bool:
        """Page through backfill or delta. Returns True when the pass finished."""
        while url:
            page = self.get(url)
            nxt, delta_link = page.get("@odata.nextLink"), page.get("@odata.deltaLink")
            try:
                with ctx.conn.transaction():
                    for root in page.get("value", []):
                        if "@removed" in root:
                            stats.gone += ctx.ingestor.mark_gone(ctx.account_id, where["folder"],
                                                                 provider_ids=[root["id"]])
                            continue
                        self._thread(ctx, tin, stats, budget, root, where, base, expanded=not delta)
                    new = {k: v for k, v in state.items() if k not in ("error", "error_at", "error_status")}
                    if delta:
                        new.update(delta_next=nxt, delta_link=delta_link or new.get("delta_link"))
                    else:
                        new.update(next_link=nxt, backfilled=not nxt)
                    ctx.cursors.put(scope, new)
                    state = new
            except Exception:
                tin.reset_caches()
                raise
            url = nxt
            if budget.done and url:
                return False
        return True

    def _thread(self, ctx, tin, stats, budget, root: dict, where: dict, base: str, *, expanded: bool) -> None:
        if root.get("replyToId"):  # a delta may list a reply on its own: file it under its thread
            conv = self._channel_conversation(where, root["replyToId"], None)
            self._count(stats, budget, tin.ingest(conv, root))
            return
        replies = root.get("replies")
        more = root.get("replies@odata.nextLink")
        root = {k: v for k, v in root.items() if not k.startswith("replies")}  # the post itself, not its replies
        conv = self._channel_conversation(where, root["id"], root.get("subject"))
        self._count(stats, budget, tin.ingest(conv, root))
        if not expanded or replies is None or more:
            replies = self.all_pages(f"{base}/{root['id']}/replies?$top={PAGE}")
        for reply in replies:
            self._count(stats, budget, tin.ingest(conv, reply))

    def _sweep(self, ctx, tin, stats, budget, scope, state, where, base, tid, cid) -> None:
        """Re-read the replies of recently active threads: a reply to an old post may not reach the delta."""
        prefix = f"channel:{tid}:{cid}:"
        rows = ctx.conn.execute(
            "select provider_thread_id from thread where account_id = %s and provider_thread_id like %s"
            " and last_at > %s order by last_at desc limit %s",
            (ctx.account_id, prefix.replace("_", r"\_").replace("%", r"\%") + "%",
             self.clock() - timedelta(days=SWEEP_DAYS), SWEEP_THREADS)).fetchall()
        for r in rows:
            root_id = r["provider_thread_id"][len(prefix):]
            replies = self.all_pages(f"{base}/{root_id}/replies?$top={PAGE}")
            try:
                with ctx.conn.transaction():
                    conv = self._channel_conversation(where, root_id, None)
                    for reply in replies:
                        self._count(stats, budget, tin.ingest(conv, reply))
            except Exception:
                tin.reset_caches()
                raise
        with ctx.conn.transaction():
            ctx.cursors.put(scope, {**(ctx.cursors.get(scope) or state), "swept_at": self.clock().isoformat()})

    @staticmethod
    def _channel_conversation(where: dict, root_id: str, subject: str | None) -> Conversation:
        meta = where["meta"]
        return Conversation(
            kind="channel", key_prefix=f"channel:{meta['team_id']}:{meta['channel_id']}",
            thread_key=f"channel:{meta['team_id']}:{meta['channel_id']}:{root_id}", folder=where["folder"],
            subject=subject or None, fallback_subject=where["label"], meta={**meta, "root_id": root_id})
