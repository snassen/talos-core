"""L3: changes to mailboxes, as reviewed batches.

    create → plan → (review) → commit → apply → next sync reconciles

create() records what was selected and what was asked for. plan() turns that into
one concrete operation per message, compared against the mirrored server state, so
no-ops are skipped and the counts shown for review are exact. commit() is the
human's approval. apply() runs the committed operations through a provider executor
in chunks, checkpointing after each, and can be resumed after a crash. Every done
operation stores its inverse, so undo() is simply another changeset.

Nothing here talks to a server by itself: executors do, and an account must have
writeback enabled in its settings before apply() will hand anything to one. No
operation can send mail or delete permanently. The worst is trash, which the
server keeps and the vault keeps regardless.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Protocol

import psycopg
from psycopg.types.json import Jsonb

from talos import rules

OPS = ("mark_read", "mark_unread", "flag", "unflag", "archive", "move", "trash", "add_label", "remove_label")
INVERSE = {"mark_read": "mark_unread", "mark_unread": "mark_read", "flag": "unflag", "unflag": "flag",
           "add_label": "remove_label", "remove_label": "add_label"}
INBOX_LABEL = "\\Inbox"
# The most operations a changeset may carry unless commit() is told otherwise. Write-back
# starts on single test messages (docs/writeback-test-plan.md) and scales 1 → 10 → 100 by
# raising this per commit, never by default.
DEFAULT_MAX_OPS = 1


class ChangesetError(RuntimeError):
    pass


class WritebackDisabled(ChangesetError):
    pass


class TooLarge(ChangesetError):
    pass


@dataclass
class Target:
    message_id: int
    folder: str
    uidvalidity: int | None
    uid: int | None
    provider_id: str | None


class Executor(Protocol):
    def apply(self, op: str, args: dict, targets: list[Target]) -> dict[int, str | None]:
        """Apply one operation to many messages. Returns message_id -> error (None when it worked)."""


class DryRunner(Protocol):
    def dry_run(self, op: str, args: dict, targets: list[Target]) -> dict[int, dict]:
        """Check the targets against the server and say what apply() would send, without sending it.
        Returns message_id -> {"ok": bool, "would": [commands], "server": {...}, "problem": str | None}."""


def create(conn: psycopg.Connection, title: str, op: str, *, args: dict | None = None,
           message_ids: list[int] | None = None, conditions: list[dict] | None = None) -> int:
    if op not in OPS:
        raise ChangesetError(f"unknown operation {op!r}")
    if (message_ids is None) == (conditions is None):
        raise ChangesetError("select messages either by id or by conditions")
    args = args or {}
    if op in ("add_label", "remove_label") and not args.get("label"):
        raise ChangesetError(f"{op} needs a label")
    if op == "move" and not args.get("folder"):
        raise ChangesetError("move needs a folder")
    selection = {"message_ids": message_ids} if message_ids is not None else {"conditions": conditions}
    return conn.execute(
        "insert into changeset (title, selection, request) values (%s, %s, %s) returning id",
        (title, Jsonb(selection), Jsonb({"op": op, "args": args}))).fetchone()["id"]


def _selected(conn: psycopg.Connection, selection: dict) -> list[int]:
    if "message_ids" in selection:
        return list(selection["message_ids"])
    where, params = rules.compile_conditions(selection["conditions"])
    return [r["id"] for r in conn.execute(f"select m.id from message m where {where}", params)]


def _account_settings(conn: psycopg.Connection) -> dict[str, dict]:
    return {r["id"]: {**r["settings"], "provider": r["provider"]}
            for r in conn.execute("select id, provider, settings from account")}


def _decide(op: str, args: dict, locs: list[dict], acct: dict) -> tuple[str, dict | None, str | None]:
    """(status, inverse, reason) for one message. status is 'pending' or 'skipped'."""
    present = [l for l in locs if l["present"]]
    if not present:
        return "skipped", None, "no longer on the server"
    flags = set().union(*(set(l["flags"]) for l in present))
    labels = set().union(*(set(l["labels"]) for l in present))
    folders = {l["folder"] for l in present}
    gmail = acct["provider"] == "gmail"
    if op in ("mark_read", "mark_unread", "flag", "unflag"):
        flag = "seen" if "read" in op else "flagged"
        has = flag in flags
        if (op in ("mark_read", "flag")) == has:
            return "skipped", None, "already so"
        return "pending", {"op": INVERSE[op], "args": {}}, None
    if op in ("add_label", "remove_label"):
        has = args["label"] in labels
        if (op == "add_label") == has:
            return "skipped", None, "already so"
        return "pending", {"op": INVERSE[op], "args": dict(args)}, None
    if op == "archive":
        if gmail:
            if INBOX_LABEL not in labels:
                return "skipped", None, "not in the inbox"
            return "pending", {"op": "add_label", "args": {"label": INBOX_LABEL}}, None
        archive = acct.get("archive_folder")
        if not archive:
            return "skipped", None, "no archive folder configured for this account"
        if archive in folders:
            return "skipped", None, "already archived"
        return "pending", {"op": "move", "args": {"folder": sorted(folders)[0]}}, None
    if op == "move":
        if args["folder"] in folders or (gmail and args["folder"] in labels):
            return "skipped", None, "already there"
        back = sorted(labels & {INBOX_LABEL}) if gmail else sorted(folders)
        return "pending", {"op": "move", "args": {"folder": back[0] if back else None}}, None
    if op == "trash":
        return "pending", {"op": "move", "args": {"folder": INBOX_LABEL if gmail and INBOX_LABEL in labels
                                                            else sorted(folders)[0], "from_trash": True}}, None
    raise ChangesetError(f"unknown operation {op!r}")


def plan(conn: psycopg.Connection, changeset_id: int) -> dict:
    """Decide, per selected message, what apply would do: pending, or skipped with a reason. Decided
    against Talos's copy of the server (message_location), never the server itself, so planning is
    free and can be repeated; each pending op stores its inverse, which is what undo later sends.
    Imported and Teams messages are always skipped: Talos has no server for the one, and never
    changes the other."""
    cs = conn.execute("select * from changeset where id = %s", (changeset_id,)).fetchone()
    if not cs:
        raise ChangesetError(f"no changeset {changeset_id}")
    if cs["status"] not in ("draft", "planned"):
        raise ChangesetError(f"changeset {changeset_id} is {cs['status']}; only a draft or planned one can be planned")
    op, args = cs["request"]["op"], cs["request"]["args"]
    ids = _selected(conn, cs["selection"])
    accounts = _account_settings(conn)
    locs: dict[int, list[dict]] = defaultdict(list)
    msg_account: dict[int, str] = {}
    for r in conn.execute(
            "select m.id, m.account_id, coalesce(l.folder_path, l.folder) as folder, l.flags, l.labels, l.present"
            " from message m"
            " left join message_location l on l.message_id = m.id where m.id = any(%s)", (ids,)):
        msg_account[r["id"]] = r["account_id"]
        if r["folder"] is not None:
            locs[r["id"]].append(r)

    summary: Counter = Counter()
    reasons: Counter = Counter()
    rows = []
    for mid in ids:
        account = msg_account.get(mid)
        if account is None:
            continue
        acct = accounts[account]
        if acct["provider"] == "local":
            status, inverse, reason = "skipped", None, "imported from disk; no server"
        elif acct["provider"] == "teams":
            status, inverse, reason = "skipped", None, "a Teams message; Talos never changes Teams"
        else:
            status, inverse, reason = _decide(op, args, locs.get(mid, []), acct)
        rows.append((changeset_id, mid, account, op, Jsonb(args), status, reason, Jsonb(inverse) if inverse else None))
        summary[f"{account}:{status}"] += 1
        if reason:
            reasons[reason] += 1
    with conn.transaction():
        conn.execute("delete from changeset_op where changeset_id = %s", (changeset_id,))
        # One batch, not one round trip per message: a structure changeset carries tens of thousands.
        if rows:
            conn.cursor().executemany(
                "insert into changeset_op (changeset_id, message_id, account_id, op, args, status, error, inverse)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s)", rows)
        result = {"op": op, "args": args, "selected": len(ids), "by_account": dict(summary),
                  "skipped_because": dict(reasons),
                  "will_change": sum(v for k, v in summary.items() if k.endswith(":pending"))}
        conn.execute("update changeset set status = 'planned', planned_at = now(), summary = %s where id = %s",
                     (Jsonb(result), changeset_id))
    return result


def commit(conn: psycopg.Connection, changeset_id: int, *, max_ops: int = DEFAULT_MAX_OPS) -> None:
    """The human's approval. After this, apply() may run it.

    Refused when the changeset would change more than max_ops messages, so a wrong
    selection cannot reach thousands of messages by accident. Raise max_ops on purpose.
    """
    pending = conn.execute("select count(*) as n from changeset_op where changeset_id = %s and status = 'pending'",
                           (changeset_id,)).fetchone()["n"]
    if pending > max_ops:
        raise TooLarge(f"changeset {changeset_id} would change {pending} messages; the limit for this commit is"
                       f" {max_ops}. Check the plan, then commit with a higher limit on purpose")
    cur = conn.execute("update changeset set status = 'committed', committed_at = now()"
                       " where id = %s and status = 'planned'", (changeset_id,))
    if cur.rowcount != 1:
        raise ChangesetError(f"changeset {changeset_id} must be planned before it is committed")


def cancel(conn: psycopg.Connection, changeset_id: int) -> None:
    conn.execute("update changeset set status = 'cancelled', finished_at = now()"
                 " where id = %s and status in ('draft', 'planned', 'committed')", (changeset_id,))


def apply(conn: psycopg.Connection, changeset_id: int, executors: dict[str, Executor], *, chunk: int = 500) -> dict:
    """Carry out a committed changeset: the only step that writes to a mail server.

    Refused unless every account with pending ops has writeback_enabled and an executor. The ops go
    out in chunks, and each chunk's results are committed before the next, so an apply that stops
    half-way (a network error, Ctrl-C) resumes where it stopped and nothing is sent twice. Every op
    gets its own result: one message failing does not fail the others. Timing per chunk and per
    connection is kept in summary.apply for the report."""
    cs = conn.execute("select * from changeset where id = %s", (changeset_id,)).fetchone()
    if not cs or cs["status"] not in ("committed", "applying"):
        raise ChangesetError(f"changeset {changeset_id} is not committed")
    accounts = _account_settings(conn)
    pending_accounts = [r["account_id"] for r in conn.execute(
        "select distinct account_id from changeset_op where changeset_id = %s and status = 'pending'", (changeset_id,))]
    for account in pending_accounts:
        if not accounts[account].get("writeback_enabled"):
            raise WritebackDisabled(f"write-back is not enabled for account {account!r}")
        if account not in executors:
            raise ChangesetError(f"no executor for account {account!r}")
    with conn.transaction():
        conn.execute("update changeset set status = 'applying' where id = %s", (changeset_id,))
        _skip_gone(conn, changeset_id)

    done = failed = 0
    started, chunks_run = time.monotonic(), []
    while True:
        batch = conn.execute(
            "select o.id, o.message_id, o.account_id, o.op, o.args, l.folder, l.uidvalidity, l.uid, l.provider_id"
            f" from changeset_op o join lateral {_LOCATION} l on true"
            " where o.changeset_id = %s and o.status = 'pending' order by o.account_id, o.id limit %s",
            (changeset_id, chunk)).fetchall()
        if not batch:
            break
        groups: dict[tuple, list] = defaultdict(list)
        for r in batch:
            groups[(r["account_id"], r["op"], repr(sorted(r["args"].items())))].append(r)
        chunk_start = time.monotonic()
        with conn.transaction():
            for (account, op, _), rows in groups.items():
                targets = [Target(r["message_id"], r["folder"], r["uidvalidity"], r["uid"], r["provider_id"]) for r in rows]
                try:
                    results = executors[account].apply(op, rows[0]["args"], targets)
                except Exception as exc:
                    results = {t.message_id: f"{type(exc).__name__}: {exc}" for t in targets}
                for r in rows:
                    err = results.get(r["message_id"], "executor gave no result")
                    conn.execute(
                        "update changeset_op set status = %s, error = %s, attempts = attempts + 1,"
                        " applied_at = case when %s then now() end where id = %s",
                        ("done" if err is None else "failed", err, err is None, r["id"]))
                    done += err is None
                    failed += err is not None
        chunks_run.append({"messages": len(batch), "seconds": round(time.monotonic() - chunk_start, 2)})
    seconds = time.monotonic() - started
    # How it went, for the report: time in all, per chunk, and what each connection did.
    timing = {"seconds": round(seconds, 2), "done": done, "failed": failed, "chunks": chunks_run,
              "per_message_ms": round(seconds * 1000 / max(1, done + failed), 1),
              "connections": {a: {k: round(v, 2) if isinstance(v, float) else v for k, v in ex.stats.items()}
                              for a, ex in executors.items() if isinstance(getattr(ex, "stats", None), dict)}}
    with conn.transaction():
        conn.execute("update changeset set status = %s, finished_at = now(),"
                     " summary = coalesce(summary, '{}'::jsonb) || jsonb_build_object('apply', %s::jsonb) where id = %s",
                     ("failed" if failed else "done", Jsonb(timing), changeset_id))
    return {"done": done, "failed": failed}


def reconcile(conn: psycopg.Connection, changeset_id: int, runners: dict) -> dict:
    """After a failed apply: ask the server (read-only) whether each failed move-like operation was
    carried out anyway (a batch answered, then the connection broke). Those are marked done, with
    their inverse kept, so an undo covers them; the rest stay failed and can be tried again."""
    rows = conn.execute(
        "select o.id, o.message_id, o.account_id, o.op, o.args, l.folder, l.uidvalidity, l.uid, l.provider_id"
        " from changeset_op o left join lateral (select l.folder, l.uidvalidity, l.uid, l.provider_id"
        "  from message_location l where l.message_id = o.message_id order by l.present desc, l.observed_at desc limit 1) l on true"
        " where o.changeset_id = %s and o.status = 'failed' and o.op in ('trash', 'archive', 'move')", (changeset_id,)).fetchall()
    groups: dict[tuple, list] = defaultdict(list)
    for r in rows:
        groups[(r["account_id"], r["op"], repr(sorted(r["args"].items())))].append(r)
    confirmed = still = 0
    with conn.transaction():
        for (account, op, _), group in groups.items():
            settled = getattr(runners.get(account), "settled", None)
            got = settled(op, group[0]["args"], [Target(r["message_id"], r["folder"], r["uidvalidity"], r["uid"], r["provider_id"])
                                                 for r in group]) if settled else {}
            for r in group:
                if got.get(r["message_id"]):
                    conn.execute("update changeset_op set status = 'done', applied_at = now(),"
                                 " error = 'confirmed on the server after an error: ' || coalesce(error, '') where id = %s", (r["id"],))
                    confirmed += 1
                else:
                    still += 1
        left = conn.execute("select count(*) as n from changeset_op where changeset_id = %s and status = 'failed'",
                            (changeset_id,)).fetchone()["n"]
        if not left:
            conn.execute("update changeset set status = 'done' where id = %s and status = 'failed'", (changeset_id,))
    return {"confirmed_done": confirmed, "still_failed": still}


def retry(conn: psycopg.Connection, changeset_id: int) -> int:
    """Failed operations back to pending, so apply() tries them again (after reconcile())."""
    with conn.transaction():
        n = conn.execute("update changeset_op set status = 'pending', error = null where changeset_id = %s and status = 'failed'",
                         (changeset_id,)).rowcount
        if n:
            conn.execute("update changeset set status = 'committed' where id = %s and status = 'failed'", (changeset_id,))
    return n


def check(conn: psycopg.Connection, changeset_id: int, runners: dict, *, sample: int | None = None) -> dict:
    """After apply: ask the server where each applied message is now (read-only), and compare with
    the mirror, which only follows at the next sync. Stored on the changeset as summary.check.

    sample reads only that many applied messages, picked at random, from the server: reading back
    thousands competes with the sync for the mailbox's request limit. The mirror is always counted
    for all of them."""
    rows = conn.execute(
        "select o.message_id, o.account_id, o.op, o.applied_at, l.folder, l.uidvalidity, l.uid, l.provider_id"
        " from changeset_op o left join lateral (select l.folder, l.uidvalidity, l.uid, l.provider_id"
        "  from message_location l where l.message_id = o.message_id order by l.observed_at desc limit 1) l on true"
        " where o.changeset_id = %s and o.status = 'done'" + (" order by random() limit %s" if sample else ""),
        (changeset_id, sample) if sample else (changeset_id,)).fetchall()
    started = time.monotonic()
    server: Counter = Counter()
    problems: Counter = Counter()
    by_account: dict[str, list] = defaultdict(list)
    for r in rows:
        by_account[r["account_id"]].append(Target(r["message_id"], r["folder"], r["uidvalidity"], r["uid"], r["provider_id"]))
    for account, targets in by_account.items():
        runner = runners.get(account)
        locate = getattr(runner, "locate", None)
        if locate is None:
            problems[f"no check for account {account}"] += len(targets)
            continue
        for loc in locate(targets).values():
            if "problem" in loc:
                problems[loc["problem"]] += 1
            else:
                server[loc["folder"]] += 1
    mirror: Counter = Counter()
    lag = []
    for r in conn.execute(
            "select o.applied_at, coalesce(l.folder_path, l.folder) as place, l.observed_at from changeset_op o"
            " left join message_location l on l.message_id = o.message_id and l.present"
            " where o.changeset_id = %s and o.status = 'done'", (changeset_id,)):
        mirror[r["place"] or "(not on the server)"] += 1
        if r["observed_at"] and r["applied_at"] and r["observed_at"] > r["applied_at"]:
            lag.append((r["observed_at"] - r["applied_at"]).total_seconds())
    result = {"at": conn.execute("select now()::text as t").fetchone()["t"], "seconds": round(time.monotonic() - started, 2),
              "checked": len(rows), "sampled": bool(sample), "server": dict(server), "problems": dict(problems), "mirror": dict(mirror),
              "mirror_caught_up": len(lag),
              "mirror_lag_s": {"min": round(min(lag)), "max": round(max(lag))} if lag else None}
    with conn.transaction():
        conn.execute("update changeset set summary = coalesce(summary, '{}'::jsonb) || jsonb_build_object('check', %s::jsonb)"
                     " where id = %s", (Jsonb(result), changeset_id))
    return result


def dry_run(conn: psycopg.Connection, changeset_id: int, runners: dict[str, DryRunner]) -> dict:
    """What apply() would send, checked against the server, with nothing sent.

    Works on a planned or committed changeset and does not need write-back to be
    enabled: the runners open folders read-only. Operation statuses are left alone;
    the report is stored on the changeset (summary.dry_run) for the UI and returned.
    """
    cs = conn.execute("select * from changeset where id = %s", (changeset_id,)).fetchone()
    if not cs or cs["status"] not in ("planned", "committed"):
        raise ChangesetError(f"changeset {changeset_id} is not planned or committed; nothing to dry-run")
    rows = conn.execute(
        "select o.id, o.message_id, o.account_id, o.op, o.args, m.subject,"
        " l.folder, l.uidvalidity, l.uid, l.provider_id"
        " from changeset_op o join message m on m.id = o.message_id"
        f" left join lateral {_LOCATION} l on true"
        " where o.changeset_id = %s and o.status = 'pending' order by o.account_id, o.id", (changeset_id,)).fetchall()
    started = time.monotonic()
    groups: dict[tuple, list] = defaultdict(list)
    for r in rows:
        groups[(r["account_id"], r["op"], repr(sorted(r["args"].items())))].append(r)
    items = []
    for (account, op, _), group in groups.items():
        present = [r for r in group if r["folder"] is not None]
        report: dict[int, dict] = {r["message_id"]: {"ok": False, "would": [], "problem": "no longer on the server"}
                                   for r in group if r["folder"] is None}
        runner = runners.get(account)
        if present and runner is None:
            report.update({r["message_id"]: {"ok": False, "would": [], "problem": f"no dry run for account {account!r}"}
                           for r in present})
        elif present:
            targets = [Target(r["message_id"], r["folder"], r["uidvalidity"], r["uid"], r["provider_id"]) for r in present]
            try:
                report.update(runner.dry_run(op, group[0]["args"], targets))
            except Exception as exc:
                report.update({t.message_id: {"ok": False, "would": [], "problem": f"{type(exc).__name__}: {exc}"}
                               for t in targets})
        for r in group:
            got = report.get(r["message_id"]) or {"ok": False, "would": [], "problem": "the dry run gave no result"}
            items.append({"message_id": r["message_id"], "account": account, "op": op, "args": r["args"],
                          "subject": r["subject"], **got})
    result = {"at": conn.execute("select now()::text as t").fetchone()["t"], "sent_nothing": True,
              "seconds": round(time.monotonic() - started, 2),
              "ok": sum(i["ok"] for i in items), "problems": sum(not i["ok"] for i in items), "items": items}
    with conn.transaction():
        conn.execute("update changeset set summary = coalesce(summary, '{}'::jsonb) || jsonb_build_object('dry_run', %s::jsonb)"
                     " where id = %s", (Jsonb(result), changeset_id))
    return result


# The location an operation acts on: one present location, or, for a restore from Trash,
# the last one known. A trashed Gmail message has left All Mail, so the sync marks its
# location gone; the restore finds it in Trash by its X-GM-MSGID, not by that location.
_LOCATION = ("(select l.folder, l.uidvalidity, l.uid, l.provider_id from message_location l"
             " where l.message_id = o.message_id"
             " and (l.present or coalesce((o.args->>'from_trash')::boolean, false))"
             " order by l.present desc, l.observed_at desc nulls last limit 1)")


def _skip_gone(conn: psycopg.Connection, changeset_id: int) -> None:
    """An operation whose message has left the server cannot be applied; say so rather than wait.
    A restore from Trash is the exception: its message has left All Mail by design."""
    conn.execute(
        "update changeset_op o set status = 'skipped', error = 'no longer on the server'"
        " where o.changeset_id = %s and o.status = 'pending'"
        " and not coalesce((o.args->>'from_trash')::boolean, false) and not exists"
        " (select 1 from message_location l where l.message_id = o.message_id and l.present)", (changeset_id,))


def undo(conn: psycopg.Connection, changeset_id: int) -> int:
    """A new, planned changeset that reverses every done operation of this one."""
    cs = conn.execute("select * from changeset where id = %s", (changeset_id,)).fetchone()
    if not cs or cs["status"] not in ("done", "failed"):
        raise ChangesetError("only an applied changeset can be undone")
    ops = conn.execute("select message_id, account_id, inverse from changeset_op"
                       " where changeset_id = %s and status = 'done' and inverse is not null", (changeset_id,)).fetchall()
    with conn.transaction():
        new_id = conn.execute(
            "insert into changeset (title, status, selection, request, planned_at, note) values"
            " (%s, 'planned', %s, %s, now(), %s) returning id",
            (f"Undo: {cs['title']}", Jsonb({"undo_of": changeset_id}), Jsonb({"op": "undo", "args": {}}),
             f"Reverses changeset {changeset_id}")).fetchone()["id"]
        for o in ops:
            conn.execute(
                "insert into changeset_op (changeset_id, message_id, account_id, op, args, status) values"
                " (%s, %s, %s, %s, %s, 'pending') on conflict do nothing",
                (new_id, o["message_id"], o["account_id"], o["inverse"]["op"], Jsonb(o["inverse"]["args"])))
        conn.execute("update changeset set summary = %s where id = %s",
                     (Jsonb({"will_change": len(ops), "undo_of": changeset_id}), new_id))
    return new_id


# ---------------------------------------------------------------------------- how the UI shows them

OPEN = ("draft", "planned", "committed", "applying")   # still asks something of the owner, or is running
# The rest (done, failed, cancelled, and "undone", which is shown but never stored) is history.


def lifecycle(rows: list[dict]) -> list[dict]:
    """Each changeset with the status to show, its group (open or history) and its undo links.

    rows carry id, status and undo_of (selection.undo_of: the changeset an undo reverses). A done
    changeset whose undo is done too is shown as "undone"; undone_by names the newest undo made of
    it, whatever that undo's own status. An undo points back with undo_of."""
    undos: dict[int, dict] = {}
    for r in sorted(rows, key=lambda r: r["id"]):
        if r.get("undo_of") is not None:
            undos[int(r["undo_of"])] = r
    out = []
    for r in rows:
        undo = undos.get(r["id"])
        shown = "undone" if r["status"] == "done" and undo and undo["status"] == "done" else r["status"]
        out.append({**r, "shown": shown, "group": "open" if shown in OPEN else "history",
                    "undone_by": undo["id"] if undo else None,
                    "undone_by_status": undo["status"] if undo else None,
                    "undo_of": int(r["undo_of"]) if r.get("undo_of") is not None else None})
    return out
