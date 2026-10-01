"""The talos command. Every mechanic is reachable from here, and scriptable.

    talos setup                       create the database, apply migrations, seed accounts, load the taxonomy
    talos status                      counts, last sync per account, secrets present or not
    talos doctor [ARGS…]              is this Mac ready, and the next step (talos-doctor; --guides, --only PHASE, --json);
                                      talos doctor pr OWNER/REPO N: screen a pull request before an agent reads it
    talos screen sources|source|import|rules|jev|report   the screen lab: injection samples from public sources,
                                      talos-doctor's rules and Jev measured on them (never prints a sample)
    talos where [--json] [--write F]  where everything is, and why: code, personal part, data, database, Keychain
                                      (names only), services, backups
    talos config init [--dir DIR]     make your personal part from the examples (never overwrites); config path
    talos backup [--status]           back up your own work now (TALOS_HOME/backups/<date>; the sync does it nightly)
    talos auth graph work             sign in to Microsoft (device code; you type the code)
    talos auth google [ACCOUNT]       sign in to Google for the calendar (consent in your browser)
    talos sync [ACCOUNT…] [--limit N] fetch new mail (read-only); one run at a time. --then-rules: afterwards
                                      rules, events, importance, the structure plan, Jev for new mail and the
                                      Studio's lifts; --no-extract: attachments later (talos extract);
                                      teams: --recent N (the N newest chats), --channel TEAM/CHANNEL
    talos retry [--account A]         re-ingest messages that failed, from their originals in the vault
    talos import PATH…                import .eml/.emlx files or folders
    talos extract                     text from attachments ingested without extraction
    talos reparse [--limit N]         re-parse messages from an older parser version, from the vault
    talos search "fakturor telia"     full-text search in Swedish and English
    talos rules run | preview JSON    recompute rule values; preview a condition list
    talos rules load FILE             save the rules of a file (idempotent; a version bump only on a change)
    talos rules remove ID [--why T]   remove a rule and the values it made (kept in rule_removed)
    talos rules remove --all [--keep ID,…] [--why T]   remove every rule but those kept
    talos rules removed               the removal history
    talos events run                  turn machine mail into events
    talos importance [--since DAYS]   score messages by importance (all, or the recent ones)
    talos importance vip-add ADDRESS  a VIP address or domain (also vip-remove, vip-list)
    talos changeset …                 create, list, plan, show, dry-run, commit [--max N], apply, check [--sample N],
                                      reconcile, retry, undo, cancel
    talos structure plan [--account A] [--dry-run]  where every mail should go under Talos/ (your structure.json);
                                      --dry-run computes and reports without storing (read-only)
    talos structure report [--account A]  the stored plan: per target, inbox before and after, To sort, old places
    talos structure why ID            the values and rules that place one message
    talos structure changesets --account gmail [--target T] [--limit N] [--new-since 1d] [--archive-unlabelled]
                                      planned Gmail changesets from the plan (add_label per target, archive);
                                      never committed: review, dry-run and commit them yourself
    talos calendar [sync|list]        copy the Microsoft 365, iCloud and Google calendars (read-only), or list them
    talos taxonomy load [PATH]        the closed value lists (default: yours, else rules/taxonomy.json) into the dimensions
    talos discovery load [DIR]        the discovery drafts (TALOS_HOME/config/discovery) into the review table;
                                      idempotent, never overwrites a decision already made
    talos discovery status            per draft: items, decided, accepted, rejected
    talos discovery export [--out DIR]  the decisions written back into the JSON files (or into DIR)
    talos enrich prepass [--dry-run] [--full]  automated reasons, subject patterns, sender profiles, origin by rules
    talos enrich report               origin coverage, pattern stats, estimated Jev cases (also as JSON in logs/)
    talos enrich gold sample [--n 300] [--seed N] [--dry-run]  a new, frozen answer key to label blind
    talos enrich gold report [--set ID] [--run RUN] [--labeller claude]  labels per field; rules (and a model run)
                                      scored against them; Claude against you where both labelled
    talos enrich gold import-labels --set ID --labeller claude FILE…  another labeller's answers (JSONL)
    talos enrich gold check-sample --set ID [--n 25] [--seed N] [--replace]  the items you check of Claude's
    talos enrich gold list            the answer keys and how far each is labelled
    talos enrich gold sample-uncertain [--n 15] [--seed N] [--fields origin,kind,topic,ask,value,route] [--dry-run]
                                      a small answer key from the cases Jev was unsure of: sender groups, single
                                      cases and Teams windows; never a message of an earlier set, never its seed
    talos enrich gold archive --set ID [--undo]  take a set off the answer-key screen (its labels stay)
    talos enrich gold propagate-groups --set ID [--apply]  your answers for each sender-group item given to every
                                      message of the group (not ticked mixed); without --apply a dry run
    talos enrich jev gold --set ID --unit message|context [--template 1|2] [--limit N] [--max-cases 400] [--run RUN]
                          [--dry-run]
                                      Jev on an answer key (evaluation only; --run resumes; --dry-run sends nothing)
    talos enrich jev report --run RUN [--compare RUN2]  Jev's accuracy, coverage, sweep and cost; two runs side by side
    talos enrich jev backfill --stage machine|person|teams [--since DAYS] [--limit N] [--max-cases 60000]
                          [--budget USD] [--concurrency 8] [--run RUN] [--dry-run]
                                      Jev over the archive in collapsed units, as proposed values (--run resumes)
    talos enrich jev backfill-report --run RUN  cases, decided share, values, proposals, propagation, cost
    talos enrich accept --run RUN[,RUN…|all] [--two-level] [--origin 0.85] [--topic 0.85] [--value 0.85] [--type 0.90]
                        [--route P] [--ask P] [--sender-kind P] [--sphere P] [--form P] [--keep P] [--kind P]
                        [--margin 0.15] [--dry-run]   the runs' proposals over the thresholds become active;
                                      --two-level: the boundaries at 0.90, kind at 0.85, exact values at 0.70 and
                                      only on their boundary's side (a type also under its kind)
    talos enrich unaccept --run RUN[,RUN…|all] [--dry-run]  put what a policy accepted back to proposed
    talos enrich propagate --run RUN[,RUN…|all] [--fields boundaries|kind|…]  (re)write a run's proposals
    talos enrich jev new [--backlog] [--since 7] [--max-cases 500] [--budget USD] [--dry-run] [--status]
                                      Jev for new mail: reuse a template's, thread's or window's answers, ask the
                                      rest (the full set, then sender_kind, kind, value), accept by the stored policy;
                                      talos sync --then-rules runs it when TALOS_HOME/enrich.json says
                                      {"enabled": true} (every 15 minutes, $0.50 a day); --backlog: all unjudged
    talos enrich jev focus --field F[,F…] --where uncertain|disagree|all [--stage S] [--since DAYS] [--max-cases N]
                          [--budget USD] [--gold] [--set 1] [--run RUN] [--dry-run]
                                      the questions a case is unsure of (F: sender_kind, kind, origin, type, topic,
                                      value, ask, route, keep; several in one combined run);
                                      --gold runs it on the answer key first and reports its accuracy
    talos cases export|import|accept  the model step as files (from before Jev; the Jev commands above replace it)
    talos export [--ai FILE]          Parquet snapshot for DuckDB, or JSONL rows for AI
    talos serve [--port 7420]         the web UI on 127.0.0.1
    talos web setup|session|sessions|signout-all  Talos Web's door: your password and authenticator (setup is
                                      yours to run), a short session file for a headless check, the sessions
    talos launchd [--web|--teams]     print (not install) the 5-minute sync job, the web server, or the Teams lane
    talos vault import [PATH] [--dry-run]  lift the Obsidian vault "Talos" into Talos Web (read-only on the vault)
    talos argus status                the watched services, their status, and the outbound heartbeat
    talos argus register [SLUG --name N --push|--probe JSON --grace S --notes T]
                                      a service (no SLUG: the day-one services that are missing)
    talos argus pause|resume SLUG     silence a service (shown as paused) or watch it again
    talos argus ack SLUG              a file_growth probe: accept the file's current size
    talos argus token [SLUG]          the curl lines a script uses to check in (the token stays in the Keychain)
    talos argus beat                  send one outbound heartbeat now
    talos argus probe [SLUG]          run the probes now (all, or one)
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import sys
from contextlib import contextmanager
from logging.handlers import WatchedFileHandler
from pathlib import Path

from talos import config, db
from talos.vault import Vault

log = logging.getLogger("talos")


class _Logfmt(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage().replace('"', "'")
        line = f'ts={self.formatTime(record, "%Y-%m-%dT%H:%M:%S")} level={record.levelname.lower()}' \
               f' logger={record.name} msg="{msg}"'
        if record.exc_info:
            line += ' exc="' + self.formatException(record.exc_info).replace("\n", " | ").replace('"', "'") + '"'
        return line


LOG_BYTES = 10 * 1024 * 1024   # talos.log is moved aside at 10 MB, keeping LOG_KEEP old files
LOG_KEEP = 5


def _rotate(path: Path, keep: int = LOG_KEEP, limit: int = LOG_BYTES) -> bool:
    """Move a log past its limit aside (log → log.1 → … → log.<keep>). Every command calls this as
    it starts, so the 5-minute sync keeps the log in bounds; the long-running processes (the web,
    the Teams lane) write through a WatchedFileHandler, which reopens the file once it has moved.
    Several processes write the same log, which is why RotatingFileHandler is not used."""
    try:
        if not path.exists() or path.stat().st_size < limit:
            return False
        for i in range(keep - 1, 0, -1):
            older = path.with_name(f"{path.name}.{i}")
            if older.exists():
                os.replace(older, path.with_name(f"{path.name}.{i + 1}"))
        os.replace(path, path.with_name(f"{path.name}.1"))
        return True
    except OSError:  # another process rotated it at the same moment
        return False


def _logging(settings: config.Settings, verbose: bool) -> None:
    """Everything to talos.log (kept in bounds by _rotate); warnings and errors to stderr.

    Under launchd, stderr is a file (launchd.err) that nothing rotates, and it used to receive a copy
    of every line in talos.log: 7 MB in a week, most of it the work account's sync saying a folder had
    nothing new. At a terminal stderr keeps showing everything, as before."""
    settings.logs.mkdir(parents=True, exist_ok=True)
    _rotate(settings.logs / "talos.log")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = _Logfmt()
    err = logging.StreamHandler(sys.stderr)
    if not (verbose or sys.stderr.isatty()):
        err.setLevel(logging.WARNING)
    for h in (err, WatchedFileHandler(settings.logs / "talos.log")):
        h.setFormatter(fmt)
        root.addHandler(h)
    for noisy in ("httpx", "httpcore", "imapclient", "msal", "pypdf"):
        logging.getLogger(noisy).setLevel(logging.ERROR if noisy == "pypdf" else logging.WARNING)


@contextmanager
def _single(settings: config.Settings, name: str):
    """Only one sync at a time: a long backfill must not collide with the 5-minute job."""
    settings.home.mkdir(parents=True, exist_ok=True)
    with open(settings.home / f"{name}.lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("another %s is running; leaving it be", name)
            yield False
            return
        yield True


def _conn(settings):
    # Autocommit, so every `with conn.transaction()` in the library is a real transaction.
    # Without it, psycopg opens an implicit transaction on the first query and each block
    # below becomes a mere savepoint: a crash hours into a backfill would lose it all.
    return db.connect(settings.dsn, autocommit=True)


def cmd_setup(a, s):
    created = db.ensure_database(s.dsn)
    from talos import accounts
    with _conn(s) as conn:
        applied = db.migrate(conn)
        accounts.seed(conn)
        conn.commit()
        tax = _setup_taxonomy(conn)
    for p in (s.vault, s.exports, s.logs):
        p.mkdir(parents=True, exist_ok=True)
    print(f"database {'created' if created else 'present'}; migrations applied: {applied or 'none'}")
    print(tax)
    print(f"data directory: {s.home}")


def _setup_taxonomy(conn) -> str:
    """Setup loads the taxonomy (yours, else rules/taxonomy.json) when it is there; a refusal is reported, not fatal."""
    from talos import taxonomy
    if not taxonomy.current().exists():
        return f"taxonomy not loaded: {taxonomy.current()} is not there (run: talos taxonomy load PATH)"
    try:
        return taxonomy.format_summary(taxonomy.load(conn))
    except taxonomy.TaxonomyError as exc:
        return f"taxonomy not loaded: {exc}"


def cmd_taxonomy(a, s):
    from talos import taxonomy
    with _conn(s) as conn:
        try:
            print(taxonomy.format_summary(taxonomy.load(conn, a.path)))
        except taxonomy.TaxonomyError as exc:
            print(f"talos: {exc}", file=sys.stderr)
            raise SystemExit(2) from None


def cmd_discovery(a, s):
    from talos import discovery
    with _conn(s) as conn:
        try:
            if a.action == "load":
                print(discovery.format_report(discovery.load(conn, a.dir)))
            elif a.action == "export":
                changed = discovery.export(conn, a.dir, out=a.out)
                where = a.out or (a.dir or discovery.default_dir())
                print("\n".join(f"{src}: {n} decisions changed in the file" for src, n in changed.items())
                      or "nothing to export: no discovery files", f"\nwritten to {where}", sep="")
            else:
                for src, v in discovery.items(conn)["sources"].items():
                    print(f"{src}: {v['decided']} of {v['total']} decided ({v['accepted']} accepted,"
                          f" {v['rejected']} rejected)")
        except discovery.DiscoveryError as exc:
            print(f"talos: {exc}", file=sys.stderr)
            raise SystemExit(2) from None


def doctor_url(remote: str | None) -> str | None:
    """talos-doctor's repository, beside this one: the same owner's talos-doctor on the same host."""
    import re
    m = re.match(r"^(?:https://|git@)([^/:]+)[/:]([^/]+)/[^/]+?(?:\.git)?/?$", (remote or "").strip())
    return f"https://{m.group(1)}/{m.group(2)}/talos-doctor" if m else None


def cmd_doctor(a, s):
    """talos-doctor, its own product (talos-doctor's repository, beside this one): installed in Talos's
    environment, else on the PATH (uv tool install), else run from its repository with uvx. It reads only."""
    import shutil
    import subprocess

    from talos import personal
    # the setup checks are told where this code is; the screen (pr, scan) takes its arguments as they are
    args = list(a.args) if a.args[:1] in (["pr"], ["scan"]) else ["--repo", str(personal.REPO), *a.args]
    try:
        from talos_doctor import cli as doctor
    except ImportError:
        doctor = None
    if doctor:
        raise SystemExit(doctor.main(args))
    if exe := shutil.which("talos-doctor"):
        raise SystemExit(subprocess.call([exe, *args]))
    remote = subprocess.run(["git", "-C", str(personal.REPO), "remote", "get-url", "origin"],
                            capture_output=True, text=True).stdout
    url = doctor_url(remote)
    if url and shutil.which("uvx"):
        print(f"(talos-doctor is not installed: running it from {url})", file=sys.stderr)
        raise SystemExit(subprocess.call(["uvx", "--from", f"git+{url}", "talos-doctor", *args]))
    print("talos-doctor is not installed. Install it from its repository (beside this one):\n"
          "    uv tool install git+<the talos-doctor repository's URL>", file=sys.stderr)
    raise SystemExit(2)


def cmd_where(a, s):
    """Where everything is, and why (talos.where). Reads only; names Keychain items, never reads one."""
    from talos import where
    sections = where.map_all(s)
    print(json.dumps(sections, ensure_ascii=False, indent=2) if a.json else where.format_text(sections))
    if a.write:
        Path(a.write).expanduser().write_text(where.format_markdown(sections), encoding="utf-8")
        print(f"written: {a.write}")


def cmd_config(a, s):
    """The personal part (talos.personal): where it is, or make it from the examples."""
    from talos import personal
    if a.config_cmd == "path":
        print(personal.home())
        return
    root = Path(a.dir).expanduser() if a.dir else personal.home()
    for name, state in personal.init(root).items():
        print(f"{state:<8} {root / name}")
    print("Edit these files to describe your accounts and yourself; nothing here may hold a password.")


def cmd_backup(a, s):
    """The backup of the owner's own work (talos.backup): make one now, or show the latest."""
    from talos import backup
    if a.status:
        m = backup.latest(s)
        if not m:
            print("no backup yet: talos backup makes one")
            return
        print(f"latest: {m['path']} ({m['made']}, {m['values']} values, {m['links']} links,"
              f" {sum(m['files'].values()) / 1e6:.1f} MB)")
        print(f"all:    {', '.join(p.name for p in backup.backups(s))}")
        return
    _backup(s, keep_days=a.keep_days, quiet=False)


def _backup(s, *, keep_days: int | None = None, quiet: bool = True) -> None:
    """Make the day's backup and check in with Argus as talos-backup; a failure is logged, never raised."""
    from talos import argus, backup
    with _single(s, "backup") as ok:
        if not ok:
            return
        try:
            m = backup.run(s, keep_days=keep_days or backup.KEEP_DAYS)
        except Exception as exc:  # noqa: BLE001 — a failed backup must never fail the sync that ran it
            log.exception("backup failed")
            argus.ping(s.dsn, backup.SLUG, ok=False, expected_next_within=backup.EVERY, summary=str(exc)[:200])
            if not quiet:
                raise SystemExit(f"talos: backup failed: {exc}") from None
            return
        size = sum(m["files"].values()) / 1e6
        line = f"backup: {m['path']} ({m['values']} values, {m['links']} links, {size:.1f} MB, {m['seconds']} s)"
        print(line + (f"; pruned {', '.join(m['pruned'])}" if m["pruned"] else ""))
        argus.ping(s.dsn, backup.SLUG, ok=True, expected_next_within=backup.EVERY,
                   summary=f"{m['values']} values, {m['links']} links, {size:.1f} MB")


def cmd_screen(a, s):
    """The screen lab (talos.screenlab): sources of injection samples and ordinary text, runs of talos-doctor's
    rules and of Jev over them, and the report. Nothing here prints a sample."""
    from talos.screenlab import manage, report, runs
    from talos.screenlab.sources import SourceError
    with _conn(s) as conn:
        try:
            if a.screen_cmd == "sources":
                for r in manage.listing(conn):
                    c = r["counts"] or {}
                    print(f"{'on ' if r['enabled'] else 'off'} {r['id']:22} cap {r['cap'] or '-':>6}  {r['license'] or '?':11}"
                          + (f" imported {c.get('imported', 0):,} ({c.get('attack', 0):,} attack, {c.get('benign', 0):,} benign,"
                             f" {c.get('duplicates', 0):,} duplicates)" if r["imported_at"] else " not imported"))
            elif a.screen_cmd == "source":
                r = manage.set_source(conn, a.id or (a.ids[0] if a.ids else ""), enabled=True if a.on else False if a.off else None, cap=a.cap,
                                      no_cap=a.no_cap)
                print(f"{r['id']}: {'on' if r['enabled'] else 'off'}, cap {r['cap'] or 'none'}")
            elif a.screen_cmd == "import":
                ids = a.ids or [r["id"] for r in manage.listing(conn) if r["enabled"]]
                if not ids:
                    print("No source is on: talos screen source ID --on")
                for sid in ids:
                    with conn.transaction():
                        c = manage.import_source(conn, sid, s.home / "screenlab" / "cache")
                    print(f"{sid}: {c['imported']:,} imported ({c['attack']:,} attack, {c['benign']:,} benign),"
                          f" {c['duplicates']:,} duplicates, {c['skipped']:,} skipped")
            elif a.screen_cmd == "rules":
                with conn.transaction():
                    r = runs.run_rules(conn, sources=a.source, limit=a.limit)
                print(f"{r['run']}: {r['samples']:,} samples judged by talos-doctor's rules")
                print(report.report(conn, r["run"]), end="")
            elif a.screen_cmd == "jev":
                est = runs.estimate_jev(conn, sources=a.source, limit=a.limit)
                print(f"Jev would judge {est['samples']:,} samples, about {est['tokens']:,} tokens: ${est['usd']:.2f}")
                if a.max_usd is None:
                    print("Nothing sent. To run: add --max-usd with a limit at or above the estimate.")
                    return
                r = runs.run_jev(conn, max_usd=a.max_usd, sources=a.source, limit=a.limit)
                conn.commit()
                print(f"{r['run']}: {r['samples']:,} judged, ${r['usd']:.4f}, {r['errors']} errors")
                print(report.report(conn, r["run"]), end="")
            elif a.screen_cmd == "report":
                print(report.report(conn, a.run, against=a.against), end="")
        except (SourceError, runs.RunError) as exc:
            raise SystemExit(f"talos: {exc}") from None


def cmd_status(a, s):
    from talos import secrets
    with _conn(s) as conn:
        for r in conn.execute(
                "select a.id, a.provider, a.enabled, a.settings, (select count(*) from message m where m.account_id = a.id) n,"
                " (select max(finished_at) from sync_run r where r.account_id = a.id and r.status in ('ok','partial')) last_ok,"
                " (select status from sync_run r where r.account_id = a.id order by id desc limit 1) last_status"
                " from account a order by a.id"):
            secret = r["settings"].get("secret")
            if r["provider"] == "graph":
                have = secrets.exists(f"graph-token-cache:{r['id']}")
            else:
                have = secrets.exists(secret) if secret else None
            print(f"{r['id']:<9} {r['provider']:<6} {'on ' if r['enabled'] else 'off'} messages={r['n']:<7}"
                  f" last_ok={r['last_ok'] or '—'} last={r['last_status'] or '—'}"
                  f" secret={'yes' if have else ('n/a' if have is None else 'MISSING')}")
        from talos.jev import KEY_NAME
        print(f"jev       key {KEY_NAME}={'yes' if secrets.exists(KEY_NAME) else 'MISSING'}")
        t = conn.execute("select (select count(*) from message) m, (select count(*) from attachment) a,"
                         " (select count(*) from person) p, (select count(*) from event) e,"
                         " (select coalesce(sum(stored_size), 0) from blob) bytes").fetchone()
        print(f"total: {t['m']} messages, {t['a']} attachments, {t['p']} people, {t['e']} events,"
              f" vault {float(t['bytes']) / 1e9:.2f} GB")


def cmd_auth(a, s):
    if a.provider == "google":
        from talos import googleauth
        account = a.account or "gmail"
        scopes = googleauth.sign_in(account)
        print(f"signed in to Google for {account}'s calendar ({scopes}); the token is in the Keychain."
              " The calendar is read at the next sync, or with Refresh on the Calendar page.")
        return
    from talos.graphauth import GraphAuth
    if not a.account:
        sys.exit("talos auth graph needs an account, e.g. talos auth graph work")
    with _conn(s) as conn:
        acct = conn.execute("select * from account where id = %s", (a.account,)).fetchone()
    if not acct or acct["provider"] != "graph":
        sys.exit(f"{a.account} is not a Microsoft Graph account")
    who = GraphAuth(acct["id"], acct["settings"]["tenant_id"], acct["settings"]["client_id"]).sign_in()
    print(f"signed in as {who}; the token cache is in the Keychain")


def cmd_sync(a, s):
    recent, channel = getattr(a, "recent", None), getattr(a, "channel", None)  # the Teams fast lane's options
    from talos import accounts
    from talos.ingest import Ingestor
    from talos.sources import base
    with _conn(s) as conn:
        rows = conn.execute("select * from account where enabled and provider <> 'local' order by id").fetchall()
        wanted = [r for r in rows if not a.accounts or r["id"] in a.accounts]
        ingestor = Ingestor(conn, Vault(s.vault), extract_inline=not a.no_extract)
        failed = False
        failures: list[str] = []
        added = 0
        from talos import secrets
        for acct in wanted:
            # An account that is not signed in yet is skipped with a note, not failed: the
            # 5-minute job should not log an error every run until the sign-in is done.
            key = (f"graph-token-cache:{acct['id']}" if acct["provider"] == "graph"
                   else acct["settings"].get("secret"))
            if key and not secrets.exists(key):
                hint = f"talos auth graph {acct['settings'].get('token_account', acct['id'])}" \
                    if acct["provider"] in ("graph", "teams") else \
                    f"security add-generic-password -U -s talos -a {key} -w"
                print(f"{acct['id']}: not signed in yet; skipped (run: {hint})")
                continue
            # One lock per account: a long backfill of one account and the 5-minute job can run
            # side by side, but never two syncs of the same account.
            with _single(s, f"sync-{acct['id']}") as ok:
                if not ok:
                    print(f"{acct['id']}: another sync is running; skipped")
                    continue
                try:
                    src = accounts.source_for(acct)
                    if acct["provider"] == "teams" and (recent or channel):
                        src = accounts.source_for(acct, recent=recent, only_channel=_teams_channel(conn, channel))
                    fast = acct["provider"] == "teams" and bool(recent or channel)
                    st = base.run(conn, ingestor, acct["id"], src, limit=a.limit, **({"quiet": True} if fast else {}))
                    added += st.added
                    print(f"{acct['id']}: seen={st.seen} added={st.added} updated={st.updated} gone={st.gone}"
                          f" failed={st.failed}"
                          + (f" ({'; '.join(st.notes)})" if st.notes else ""))
                except Exception as exc:
                    failed = True
                    failures.append(acct["id"])
                    print(f"{acct['id']}: FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        if recent and "teams" in [x["id"] for x in wanted]:
            _teams_fast_done(conn, s, ok=not failed, added=added)
        if a.then_rules:
            from talos import events, rules
            with _single(s, "rules") as ok:
                if ok:
                    rules.run_all(conn)
                    events.run(conn)
                    if added:  # origin reads types, events, replies and profiles, which new mail changes
                        from talos import enrich
                        try:
                            res = enrich.prepass(conn)
                            print(f"enrich: origin +{res['origin']['added']} -{res['origin']['removed']}"
                                  f" in {res['seconds']['total']} s")
                        except Exception as exc:  # the synced mail is committed; importance still runs
                            failed = True
                            failures.append("enrich prepass")
                            log.exception("enrich prepass failed")
                            print(f"enrich: FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
                    from talos import importance  # the last 30 days, after rules and events
                    importance.compute_recent(conn, days=30)
                    # New mail into the structure plan (accounts that have one). Never raises and
                    # makes no changeset: preparing one is the owner's step (talos structure changesets).
                    from talos import structure
                    placed = structure.after_sync(conn)
                    if placed:
                        print("structure: " + ", ".join(f"{k} {v['placed']} placed ({v['mode']})"
                                                        for k, v in placed.items()))
                    # Jobs & fruit's low-hanging fruit, computed again for the new mail (never raises).
                    from talos import unlock
                    unlock.refresh_fruit(conn)
            # The Microsoft 365 calendars, copied read-only for the Calendar page (a full run only).
            # Never raises: sync_all reports a failing account and the mail sync goes on.
            if not a.accounts:
                from talos import calendars
                with _single(s, "calendar") as ok:
                    if ok:
                        for acct, res in calendars.sync_all(conn).items():
                            print(f"calendar {acct}: " + (f"FAILED {res['error']}" if "error" in res else
                                  f"{res['calendars']} calendars, {res['entries']} entries, {res['gone']} gone"
                                  + (f", failed: {', '.join(res['failed'])}" if res["failed"] else "")))
            # Jev's judgement for new mail, when enrich.json turns it on: at most every 15 minutes,
            # within its daily budget. Never raises, and never fails the sync; it checks in with
            # Argus as talos-enrich itself.
            try:
                from talos import incremental
                with _single(s, "enrich-new") as ok:
                    if ok:
                        res = incremental.after_sync(conn, s.home, s.dsn)
                        if res and "selected" in res:
                            print("enrich new: " + incremental.format_line(res))
            except Exception:  # noqa: BLE001 — belt and braces: after_sync itself never raises
                log.exception("enrich new: the hook failed; the sync is not affected")
            # The kinds of Jev guess the Studio lifted take new mail's guesses too (talos.studio.relift).
            try:
                from talos import studio
                res = studio.relift(conn)
                conn.commit()
                if res["messages"]:
                    print(f"studio: {res['messages']} new messages lifted")
            except Exception:  # noqa: BLE001 — never fails the sync
                conn.rollback()
                log.exception("studio relift failed; the sync is not affected")
    if not a.accounts:
        # Argus: a full run checks in as talos-sync, in-process. ping() never raises and gives up
        # after 3 seconds, so a failure to record it can never fail or hold up the sync.
        from talos import argus
        argus.ping(s.dsn, "talos-sync", ok=not failed, expected_next_within=argus.SYNC_EVERY,
                   summary=f"failed: {', '.join(failures)}" if failures else
                   f"{len(wanted)} accounts, {added} new messages")
        # The nightly backup of the owner's own work: the first full sync after 02:00 (talos.backup).
        from talos import backup
        if a.then_rules and backup.due(s):
            _backup(s)
    if failed:
        sys.exit(1)


TEAMS_PING_EVERY = 240   # seconds between the fast lane's Argus check-ins (it runs every 20)


def _teams_fast_done(conn, s, *, ok: bool, added: int) -> None:
    """After a fast-lane run: when it looked (the Teams page shows it), and, at most every
    TEAMS_PING_EVERY seconds, a check-in with Argus as talos-teams, so a stopped service is noticed."""
    from datetime import datetime, timezone

    from psycopg.types.json import Jsonb
    now = datetime.now(timezone.utc)
    prev = conn.execute("select state from sync_cursor where account_id = 'teams' and scope = 'fast'").fetchone()
    state = dict((prev or {}).get("state") or {})
    ping = ok and (not state.get("pinged_at") or (now - datetime.fromisoformat(state["pinged_at"])).total_seconds() >= TEAMS_PING_EVERY)
    state.update(checked_at=now.isoformat(), added=added, ok=ok, **({"pinged_at": now.isoformat()} if ping else {}))
    with conn.transaction():
        conn.execute("insert into sync_cursor (account_id, scope, state) values ('teams', 'fast', %s)"
                     " on conflict (account_id, scope) do update set state = excluded.state, updated_at = now()", (Jsonb(state),))
    if ping or not ok:
        from talos import argus
        argus.ping(s.dsn, "talos-teams", ok=ok, expected_next_within=600,
                   summary=f"new chat messages: {added}" if ok else "the fast lane failed")


def _teams_channel(conn, spec: str | None) -> dict | None:
    """--channel TEAM_ID/CHANNEL_ID as the Teams source takes it, with the names the synced messages carry."""
    if not spec:
        return None
    tid, _, cid = spec.partition("/")
    row = conn.execute("select headers->>'x-teams-team' as team, headers->>'x-teams-channel' as channel from message"
                       " where headers->>'x-teams-team-id' = %s and headers->>'x-teams-channel-id' = %s limit 1",
                       (tid, cid)).fetchone()
    return {"team": {"id": tid, "displayName": row["team"] if row else tid},
            "channel": {"id": cid, "displayName": row["channel"] if row else cid}}


def cmd_import(a, s):
    from talos.ingest import Ingestor
    from talos.sources import base
    from talos.sources.local import LocalSource
    with _conn(s) as conn:
        ingestor = Ingestor(conn, Vault(s.vault))
        st = base.run(conn, ingestor, a.account, LocalSource([Path(p) for p in a.paths], folder=a.folder))
        print(f"imported: seen={st.seen} added={st.added}")


def cmd_retry(a, s):
    from talos.ingest import retry_failures
    with _conn(s) as conn:
        print(retry_failures(conn, Vault(s.vault), account_id=a.account))


def cmd_extract(a, s):
    from talos.ingest import extract_pending
    with _conn(s) as conn:
        total = 0
        while True:
            n = extract_pending(conn, Vault(s.vault), limit=200)
            conn.commit()
            total += n
            if n == 0 or (a.limit and total >= a.limit):
                break
        print(f"extracted {total} attachments")


def cmd_reparse(a, s):
    from talos import mime, reparse
    with _single(s, "reparse") as ok:
        if not ok:
            print("another reparse is running; skipped")
            return
        with _conn(s) as conn:
            res = reparse.reparse(conn, Vault(s.vault), limit=a.limit)
    print(f"reparsed {res['reparsed']} messages to parser version {mime.PARSER_VERSION}; failed {res['failed']};"
          f" still older: {res['remaining']}")


def cmd_search(a, s):
    from talos import search
    with _conn(s) as conn:
        for r in search.messages(conn, a.query, limit=a.limit)["rows"]:
            when = r["received_at"].strftime("%Y-%m-%d") if r["received_at"] else "????-??-??"
            print(f"{r['id']:>8}  {when}  {(r['from_address'] or '')[:32]:<32}  {r['subject'] or ''}")


def cmd_rules(a, s):
    from talos import rules
    with _conn(s) as conn:
        try:
            _rules(a, conn, rules)
        except rules.RuleError as exc:
            conn.rollback()
            print(f"talos: {exc}", file=sys.stderr)
            raise SystemExit(2) from None


def _rules(a, conn, rules):
    if a.action == "run":
        print(json.dumps(rules.run_all(conn), indent=2))
        conn.commit()
    elif a.action == "preview":
        res = rules.preview(conn, json.loads(a.json))
        print(f"{res['count']} messages")
        for r in res["sample"]:
            print(f"  {r['id']:>8}  {r['from_address']}  {r['subject']}")
    elif a.action in ("load", "add"):  # add: the old name
        if not a.json:
            raise rules.RuleError("usage: talos rules load FILE")
        done = rules.load_file(conn, a.json)
        conn.commit()
        for r in done:
            print(f"{r['state']:<12} {r['id']}@{r['version']}{'' if r['enabled'] else '  (off)'}")
        n = {k: sum(1 for r in done if r["state"] == k) for k in ("created", "new version", "updated", "unchanged")}
        print(", ".join(f"{v} {k}" for k, v in n.items() if v) + ". Run `talos rules run` to apply them.")
    elif a.action == "remove":
        if a.all == bool(a.json):
            raise rules.RuleError("usage: talos rules remove ID [--why TEXT], or talos rules remove --all [--keep ID,…]")
        if a.all:
            keep = [k.strip() for k in (a.keep or "").split(",") if k.strip()]
            done = rules.remove_all(conn, keep=keep, why=a.why)
        else:
            if a.keep:
                raise rules.RuleError("--keep goes with --all")
            done = [rules.remove(conn, a.json, why=a.why)]
        conn.commit()
        for r in done:
            print(f"removed {r['id']}@{r['version']}: {r['values']} values, {r['edges']} memberships")
        print(f"{len(done)} rule{'' if len(done) == 1 else 's'} removed; kept in rule_removed."
              " Run `talos rules run` so the other rules fill in.")
    elif a.action == "removed":
        for r in rules.removed(conn):
            d = r["definition"]
            print(f"{r['removed_at']:%Y-%m-%d %H:%M}  {r['rule_id']}@{d.get('version')}  {d.get('name')}"
                  + (f"  ({r['why']})" if r["why"] else ""))


def cmd_events(a, s):
    from talos import events
    with _conn(s) as conn:
        print(json.dumps(events.run(conn), indent=2))
        conn.commit()


def cmd_calendar(a, s):
    from talos import calendars
    with _conn(s) as conn:
        if a.action == "sync":
            print(json.dumps(calendars.sync_all(conn), indent=2))
        else:
            print(json.dumps({"calendars": calendars.list_calendars(conn), "synced": calendars.last_sync(conn)}, indent=2))
        conn.commit()


def cmd_importance(a, s):
    from datetime import datetime, timedelta, timezone

    from talos import importance
    with _conn(s) as conn:
        if a.action == "vip-list":
            for r in importance.vip_list(conn):
                print(f"{r['pattern']}" + (f"  ({r['note']})" if r["note"] else ""))
            return
        if a.action in ("vip-add", "vip-remove"):
            if not a.value:
                sys.exit(f"talos importance {a.action} needs an address or a domain")
            fn = importance.vip_add if a.action == "vip-add" else importance.vip_remove
            print(json.dumps(fn(conn, a.value), indent=2))
            return
        with _single(s, "rules") as ok:  # never beside a rules run, which rewrites the values it reads
            if not ok:
                print("a rules run is in progress; skipped")
                return
            since = datetime.now(timezone.utc) - timedelta(days=a.since) if a.since else None
            print(json.dumps(importance.compute(conn, since=since), indent=2))


def cmd_changeset(a, s):
    from talos import changesets
    with _conn(s) as conn:
        if a.action in ("plan", "show", "commit", "apply", "check", "reconcile", "retry", "dry-run", "undo", "cancel") and a.id is None:
            raise SystemExit(f"talos changeset {a.action} needs a changeset id")
        if a.action == "create":
            if a.ids_file:
                from pathlib import Path
                a.ids = ",".join(x for x in Path(a.ids_file).read_text().replace(",", " ").split() if x)
            if bool(a.ids) == bool(a.where):
                raise SystemExit("select messages with --ids 1,2,3, --ids-file or --where '<JSON conditions>', one of them")
            ids = [int(x) for x in a.ids.split(",")] if a.ids else None
            cid = changesets.create(conn, a.title, a.op, args=json.loads(a.args or "{}"), message_ids=ids,
                                    conditions=json.loads(a.where) if a.where else None)
            conn.commit()
            print(f"changeset {cid} created; next: talos changeset plan {cid}")
        elif a.action == "list":
            for r in conn.execute("select id, status, title, summary->>'will_change' as n from changeset"
                                  " order by id desc limit 30"):
                print(f"{r['id']:>5}  {r['status']:<10} {r['n'] or '-':>6}  {r['title']}")
        elif a.action == "dry-run":
            from talos.writeback import close_all, dry_runners_for
            runners = dry_runners_for(conn)
            try:
                report = changesets.dry_run(conn, a.id, runners)
            except changesets.ChangesetError as exc:
                raise SystemExit(f"refused: {exc}") from None
            finally:
                close_all(runners)
            conn.commit()
            _print_dry_run(a.id, report)
        elif a.action == "undo":
            try:
                new_id = changesets.undo(conn, a.id)
            except changesets.ChangesetError as exc:
                raise SystemExit(f"refused: {exc}") from None
            conn.commit()
            print(f"changeset {new_id} reverses {a.id} and is planned. Next: talos changeset dry-run {new_id},"
                  f" then commit and apply it.")
        elif a.action == "cancel":
            changesets.cancel(conn, a.id)
            conn.commit()
            print(f"changeset {a.id} cancelled (if it had not been applied)")
        elif a.action == "plan":
            print(json.dumps(changesets.plan(conn, a.id), indent=2, ensure_ascii=False))
            conn.commit()
        elif a.action == "show":
            cs = conn.execute("select * from changeset where id = %s", (a.id,)).fetchone()
            if not cs:
                raise SystemExit(f"no changeset {a.id}")
            cs["ops"] = conn.execute(
                "select o.message_id, m.subject, o.account_id, o.op, o.args, o.status, o.error, o.inverse"
                " from changeset_op o join message m on m.id = o.message_id where o.changeset_id = %s"
                " order by o.id limit 50", (a.id,)).fetchall()
            print(json.dumps({k: str(v) if k.endswith("_at") else v for k, v in cs.items()}, indent=2,
                             ensure_ascii=False, default=str))
        elif a.action == "commit":
            try:
                changesets.commit(conn, a.id, max_ops=a.max)
            except changesets.ChangesetError as exc:
                raise SystemExit(f"refused: {exc}") from None
            conn.commit()
            print(f"changeset {a.id} committed; nothing was sent to a server yet. To write it: talos changeset apply {a.id}")
        elif a.action == "apply":
            _apply_changeset(conn, a.id)
        elif a.action == "reconcile":
            from talos.writeback import close_all, dry_runners_for
            runners = dry_runners_for(conn)
            try:
                res = changesets.reconcile(conn, a.id, runners)
            finally:
                close_all(runners)
            conn.commit()
            print(json.dumps(res, indent=2))
        elif a.action == "retry":
            n = changesets.retry(conn, a.id)
            conn.commit()
            print(f"{n} failed operation(s) of changeset {a.id} are pending again; next: talos changeset apply {a.id}")
        elif a.action == "check":
            from talos.writeback import close_all, dry_runners_for
            runners = dry_runners_for(conn)
            try:
                res = changesets.check(conn, a.id, runners, sample=a.sample)
            finally:
                close_all(runners)
            conn.commit()
            print(json.dumps(res, indent=2, ensure_ascii=False))


def cmd_structure(a, s):
    from talos import structure
    try:
        rules = structure.load(a.rules)
        with _conn(s) as conn:
            if a.action == "plan":
                if a.dry_run:
                    print(structure.format_report(structure.preview(conn, rules, account=a.account)))
                    print("\n(dry run: computed, not stored)")
                    return
                res = structure.plan(conn, rules, account=a.account)
                print(structure.format_report(res))
                for acc, r in res.items():
                    print(f"{acc}: {r['placed']:,} placed in {r['seconds']} s ({r['with_report_seconds']} s with the report)")
                print("\nNothing was written to a mailbox. Next, for Gmail: talos structure changesets --account gmail")
            elif a.action == "report":
                rep = structure.report(conn, a.account)
                if not rep:
                    raise SystemExit("no plan yet: talos structure plan")
                print(structure.format_report({k: v["summary"] for k, v in rep.items() if v["summary"]}))
                for k, v in rep.items():
                    if v["last_run"]:
                        print(f"{k}: last run {v['last_run']['started_at']:%Y-%m-%d %H:%M} ({v['last_run']['mode']})")
            elif a.action == "why":
                if not a.id:
                    raise SystemExit("talos structure why needs a message id")
                print(json.dumps(structure.why(conn, a.id, rules), indent=2, ensure_ascii=False, default=str))
            elif a.action == "changesets":
                if not a.account:
                    raise SystemExit("talos structure changesets needs --account (gmail)")
                since = structure.parse_since(a.new_since) if a.new_since else None
                made = structure.changesets(conn, a.account, target=a.target, limit=a.limit, new_since=since,
                                            archive_unlabelled=a.archive_unlabelled, rules=rules)
                for c in made:
                    if "note" in c:
                        print(f"note: {c['note']}")
                        continue
                    skipped = ", ".join(f"{v} {k}" for k, v in c["skipped_because"].items())
                    print(f"changeset {c['id']:>5}  planned  {c['title']}" + (f"  (skipped: {skipped})" if skipped else ""))
                if not [c for c in made if "id" in c]:
                    print("nothing to prepare: every planned label is on the server, or already in an open changeset")
                print("\nNothing is committed. For each: talos changeset dry-run ID, then commit with --max N"
                      " (1, then 10, 100, all) and apply.")
    except structure.StructureError as exc:
        raise SystemExit(f"talos structure: {exc}") from None


def _print_dry_run(changeset_id: int, report: dict) -> None:
    print(f"dry run of changeset {changeset_id}: nothing was sent. {report['ok']} ready, {report['problems']} with a problem"
          f" ({report.get('seconds', '?')} s)")
    shown = report["items"] if len(report["items"]) <= 25 else [i for i in report["items"] if i["problem"]][:25] + report["items"][:3]
    if len(shown) < len(report["items"]):
        print(f"  (showing the problems and the first three of {len(report['items'])})")
    for i in shown:
        print(f"\n  message {i['message_id']} ({i['account']}): {i['subject']!r}")
        print(f"    op: {i['op']} {json.dumps(i['args'], ensure_ascii=False) if i['args'] else ''}")
        if i.get("server"):
            srv = i["server"]
            print(f"    server now: {srv.get('folder')}" + (f" UID {srv['uid']}" if srv.get("uid") else "")
                  + (f", flags {srv['flags']}, labels {srv['labels']}" if "flags" in srv else ""))
        for c in i["would"]:
            print(f"    would send: {c}")
        if i["problem"]:
            print(f"    PROBLEM: {i['problem']}")


def _apply_changeset(conn, changeset_id: int) -> None:
    """Hand a committed changeset to the executors. Refuses clearly when write-back is off."""
    from talos import changesets
    from talos.writeback import close_all, executors_for
    executors = executors_for(conn)
    try:
        result = changesets.apply(conn, changeset_id, executors)
    except changesets.WritebackDisabled as exc:
        print(f"refused: {exc}.\n"
              "Write-back is off for that account, so nothing was sent to a server. Switching it on is a"
              " deliberate, per-account decision (account.settings.writeback_enabled).", file=sys.stderr)
        raise SystemExit(2) from None
    except changesets.ChangesetError as exc:
        print(f"refused: {exc}. Nothing was sent to a server.", file=sys.stderr)
        raise SystemExit(2) from None
    finally:
        close_all(executors)
    timing = conn.execute("select summary->'apply' as t from changeset where id = %s", (changeset_id,)).fetchone()["t"]
    print(json.dumps({"changeset": changeset_id, **result, "timing": timing}, indent=2))
    print(f"Next: talos changeset check {changeset_id} (where the server has them now), then talos sync <account>.")


def cmd_enrich(a, s):
    from talos import enrich
    with _conn(s) as conn:
        if a.action == "prepass":
            with _single(s, "rules") as ok:  # never beside a rules run: both write rule-made values
                if not ok:
                    print("a rules run is in progress; skipped")
                    return
                res = enrich.prepass(conn, dry_run=a.dry_run, full=a.full)
            print(json.dumps(res, indent=2, ensure_ascii=False))
            if a.dry_run:
                print("dry run: everything was rolled back")
        elif a.action == "report":
            rep = enrich.report(conn)
            print(enrich.format_report(rep))
            print(f"\nreport: {enrich.write_report(rep, s.logs)}")
        elif a.action == "gold":
            if a.gold == "gold":
                print("talos: usage: talos enrich gold sample|sample-uncertain|report|list|import-labels|check-sample"
                      "|archive|propagate-groups", file=sys.stderr)
                raise SystemExit(2)
            cmd_gold(a, s, conn)
        elif a.action == "jev":
            if a.gold in ("backfill", "backfill-report"):
                cmd_backfill(a, s, conn)
            elif a.gold == "focus":
                cmd_focus(a, s, conn)
            elif a.gold == "new":
                cmd_new(a, s, conn)
            else:
                cmd_jev(a, s, conn)
        elif a.action in ("accept", "unaccept", "propagate"):
            cmd_accept(a, s, conn)


def cmd_backfill(a, s, conn):
    """The enrichment backfill (stages C–E): a run, a dry run, or its report."""
    from datetime import datetime

    from talos import backfill, jev
    try:
        if a.gold == "backfill-report":
            if not a.run:
                raise backfill.BackfillError("usage: talos enrich jev backfill-report --run RUN")
            rep = backfill.report(conn, a.run)
            print(backfill.format_report(rep))
            s.logs.mkdir(parents=True, exist_ok=True)
            out = s.logs / f"backfill-report-{a.run}-{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
            out.write_text(json.dumps(rep, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            print(f"\nreport: {out}")
            return
        if not a.stage:
            raise backfill.BackfillError("usage: talos enrich jev backfill --stage machine|person|teams [--since DAYS]"
                                         " [--limit N] [--max-cases N] [--budget USD] [--run RUN] [--dry-run]")
        client = None if a.dry_run else jev.JevClient(concurrency=a.concurrency or jev.CONCURRENCY)
        res = backfill.run(conn, a.stage, client=client, run_id=a.run, since=a.since, limit=a.limit,
                           max_cases=a.max_cases if a.max_cases is not None else backfill.MAX_CASES,
                           budget=a.budget, dry_run=a.dry_run)
        if a.dry_run:
            print(backfill.format_dry_run(res))
            return
        u, pr = res["usage"], res["propagation"]
        print(f"run {res['run_id']}: {res['stored']:,} stored, {len(res['failed']):,} failed;"
              f" {u['requests']:,} requests ({u['retries']:,} retries), {u['input_tokens']:,} input tokens"
              f" (${res['cost_usd']:.2f}), {res['seconds']} s")
        print(f"proposals: {pr['written']:,} written ({pr['replaced']:,} replaced) in {pr['seconds']} s;"
              " per field " + ", ".join(f"{f} {n:,}" for f, n in pr["rows_by_field"].items()))
        if pr["templates"]:
            print("templates agreed/split: " + ", ".join(f"{f} {d['agreed']:,}/{d['split']:,}"
                                                          for f, d in pr["templates"].items()))
        for f in res["failed"][:20]:
            print(f"  {f['unit']}: {f['error']}")
        if res.get("error"):
            print(f"talos: stopped: {res['error']}", file=sys.stderr)
        if res["failed"] or res.get("error"):
            print(f"resume with: talos enrich jev backfill --stage {a.stage} --run {res['run_id']}"
                  + (f" --since {a.since}" if a.since else "") + (f" --limit {a.limit}" if a.limit is not None else ""))
            raise SystemExit(1)
        print(f"report: talos enrich jev backfill-report --run {res['run_id']}")
        print(f"accept: talos enrich accept --run {res['run_id']} --dry-run")
    except (backfill.BackfillError, jev.JevError) as exc:
        print(f"talos: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def cmd_focus(a, s, conn):
    """A focused Jev run: one question per case (talos.focus), on the answer key (--gold) or the archive."""
    from talos import focus, jev
    try:
        if not a.field or (not a.where and not a.focus_gold):
            raise focus.FocusError("usage: talos enrich jev focus --field sender_kind|kind|origin|type|topic|value|ask|route"
                                   "[,…] --where uncertain|disagree|all [--stage S] [--since DAYS] [--max-cases N]"
                                   " [--budget USD] [--run RUN] [--dry-run]; or --gold [--set 1] first")
        fields = focus.parse_fields(a.field)
        many = len(fields) > 1 or fields[0] in focus.ASKED
        client = None if a.dry_run else jev.JevClient(concurrency=a.concurrency or jev.CONCURRENCY)
        if a.focus_gold:
            res = focus.gold_run(conn, a.set or 1, fields if many else fields[0], client=client, dry_run=a.dry_run)
            print(focus.format_gold(res))
            return
        go = focus.run_many if many else focus.run
        res = go(conn, fields if many else fields[0], a.where, stages=[a.stage] if a.stage else None, since=a.since,
                 client=client, run_id=a.run, max_cases=a.max_cases if a.max_cases is not None else focus.MAX_CASES,
                 budget=a.budget, dry_run=a.dry_run)
        if a.dry_run:
            print(focus.format_dry_run_many(res) if many else focus.format_dry_run(res))
            return
        u, pr = res["usage"], res["propagation"]
        print(f"run {res['run_id']}: {res['stored']:,} stored, {len(res['failed']):,} failed; {u['requests']:,} requests,"
              f" {u['input_tokens']:,} input tokens (${res['cost_usd']:.2f}), {res['seconds']} s")
        print(f"proposals: {pr['written']:,} written, {pr['superseded']:,} older ones superseded; per field "
              + ", ".join(f"{f} {n:,}" for f, n in pr["rows_by_field"].items()))
        if res.get("error"):
            print(f"talos: stopped: {res['error']}", file=sys.stderr)
            raise SystemExit(1)
        print("accept: talos enrich accept --run all --two-level --dry-run")
    except (focus.FocusError, jev.JevError, ValueError) as exc:
        print(f"talos: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def cmd_new(a, s, conn):
    """Incremental enrichment (talos.incremental): Jev's judgement for what is new, by hand."""
    from talos import incremental, jev
    try:
        cfg = incremental.settings(s.home)
        if a.status:
            print(incremental.format_status(cfg, conn))
            return
        client = None if a.dry_run else jev.JevClient(concurrency=a.concurrency or jev.CONCURRENCY)
        kw = dict(client=client, backlog=a.backlog, since_days=a.since or int(cfg["since_days"]),
                  max_cases=a.max_cases, budget=a.budget, accept_policy=cfg.get("accept"))
        if a.dry_run:
            print(incremental.format_dry_run(incremental.run(conn, dry_run=True, **kw)))
            return
        with _single(s, "enrich-new") as ok:
            if not ok:
                print("another talos enrich jev new is running; skipped")
                return
            res = incremental.run(conn, trigger="cli", **kw)
        print(incremental.format_run(res))
        if res.get("error"):
            print(f"talos: stopped: {res['error']}", file=sys.stderr)
            raise SystemExit(1)
    except (incremental.IncrementalError, jev.JevError) as exc:
        print(f"talos: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def cmd_accept(a, s, conn):
    from talos import backfill
    try:
        if not a.run:
            raise backfill.BackfillError(f"usage: talos enrich {a.action} --run RUN[,RUN…|all] [--dry-run]")
        runs = backfill.runs_of(conn, a.run)
        if a.action == "unaccept":
            res = backfill.unaccept(conn, runs, dry_run=a.dry_run)
            print(("DRY RUN (rolled back): " if a.dry_run else "") + f"run {res['run_id']}: back to proposed: "
                  + (", ".join(f"{f} {n:,}" for f, n in res["fields"].items()) or "nothing"))
            return
        if a.action == "propagate":
            fields = [f.strip() for f in a.fields.split(",")] if a.fields else None
            if fields == ["boundaries"]:
                fields = list(backfill.boundary.FIELDS)
            for r in runs:
                res = backfill.propagate(conn, r, fields=fields)
                print(f"run {r}: {res['written']:,} proposals written ({res['replaced']:,} replaced),"
                      f" {res['superseded']:,} older ones superseded; per field "
                      + ", ".join(f"{f} {n:,}" for f, n in res["rows_by_field"].items()))
            return
        # The preset: the old per-field one (origin, topic, value 0.85, type 0.90) unless --two-level;
        # a field given on the command line overrides it.
        thresholds = {**backfill.PRESETS["two-level" if a.two_level else "per-field"]}
        for f in backfill.ACCEPT_FIELDS:
            v = getattr(a, f"accept_{f}")
            if v is not None:
                thresholds[f] = v
        res = backfill.accept(conn, runs, thresholds, margin=a.margin if a.margin is not None
                              else backfill.ACCEPT_MARGIN, dry_run=a.dry_run)
        print(backfill.format_accept(res))
        if not a.dry_run:  # what is decided changed: Jobs & fruit follows (never raises)
            from talos import unlock
            unlock.refresh_fruit(conn)
    except backfill.BackfillError as exc:
        print(f"talos: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def cmd_jev(a, s, conn):
    """Jev on the answer key (step C): a run, a dry run, or the report. Evaluation only."""
    from datetime import datetime

    from talos import jev, jev_gold
    try:
        if a.gold == "gold":
            if not a.set or not a.unit:
                raise jev_gold.JevRunError("usage: talos enrich jev gold --set ID --unit message|context"
                                           " [--limit N] [--max-cases N] [--run RUN] [--dry-run]")
            client = None if a.dry_run else jev.JevClient(concurrency=a.concurrency or jev.CONCURRENCY)
            res = jev_gold.run(conn, a.set, a.unit, client=client, run_id=a.run, limit=a.limit,
                               max_cases=a.max_cases if a.max_cases is not None else jev_gold.MAX_CASES,
                               dry_run=a.dry_run, template=a.template or jev.TEMPLATE_VERSION)
            if a.dry_run:
                print(jev_gold.format_dry_run(res))
                return
            u = res["usage"]
            print(f"run {res['run_id']}: {res['stored']} stored, {len(res['failed'])} failed;"
                  f" {u['requests']} requests ({u['retries']} retries), {u['input_tokens']:,} input tokens"
                  f" (${res['cost_usd']:.4f}), {res['seconds']} s")
            for f in res["failed"][:20]:
                print(f"  item {f['position']}: {f['error']}")
            if res.get("error"):
                print(f"talos: stopped: {res['error']}", file=sys.stderr)
            if res["failed"] or res.get("error"):
                print(f"resume with: talos enrich jev gold --set {a.set} --unit {a.unit} --run {res['run_id']}"
                      + (f" --template {a.template}" if a.template else "")
                      + (f" --limit {a.limit}" if a.limit is not None else ""))
                raise SystemExit(1)
            print(f"report: talos enrich jev report --run {res['run_id']}")
        elif a.gold == "report":
            if not a.run:
                raise jev_gold.JevRunError("usage: talos enrich jev report --run RUN [--compare RUN2]")
            rep = jev_gold.report(conn, a.run, compare_run=a.compare)
            print(jev_gold.format_report(rep))
            s.logs.mkdir(parents=True, exist_ok=True)
            out = s.logs / f"jev-report-{a.run}-{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
            out.write_text(json.dumps(rep, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            print(f"\nreport: {out}")
        else:
            raise jev_gold.JevRunError("usage: talos enrich jev gold … | talos enrich jev report --run RUN")
    except (jev_gold.JevRunError, jev.JevError) as exc:
        print(f"talos: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def cmd_gold(a, s, conn):
    from datetime import datetime

    from talos import gold
    try:
        if a.gold in ("sample", "report", "sample-uncertain", "archive", "propagate-groups") and a.files:
            raise gold.GoldError(f"unexpected arguments: {' '.join(a.files)}")
        if a.gold == "sample":
            res = gold.sample(conn, n=a.n or 300, seed=a.seed, name=a.name, dry_run=a.dry_run)
            print(json.dumps(res, indent=2, ensure_ascii=False))
            print("dry run: nothing was stored" if a.dry_run else
                  f"answer key {res['set_id']}: {res['items']} items; label them in Talos under Operations › Answer key")
        elif a.gold == "sample-uncertain":
            res = gold.sample_uncertain(conn, n=a.n or 15, seed=a.seed, name=a.name,
                                        fields=gold.check_fields(a.fields) if a.fields else gold.FIELDS,
                                        dry_run=a.dry_run)
            print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
            print("dry run: nothing was stored" if a.dry_run else
                  f"answer key {res['set_id']} (seed {res['seed']}): {res['items']} items labelling"
                  f" {', '.join(res['fields'])}; label them in Talos under Operations › Answer key")
        elif a.gold == "archive":
            if not a.set:
                raise gold.GoldError("usage: talos enrich gold archive --set ID [--undo]")
            res = gold.archive(conn, a.set, not a.undo)
            print(f"answer key {res['set_id']} ({res['name']}): " + ("archived: off the screen, its labels kept"
                                                                     if res["archived"] else "back on the screen"))
        elif a.gold == "propagate-groups":
            if not a.set:
                raise gold.GoldError("usage: talos enrich gold propagate-groups --set ID [--apply]")
            res = gold.propagate_groups(conn, a.set, labeller=a.labeller or gold.OWNER, dry_run=not a.apply)
            print(gold.format_propagate(res))
        elif a.gold == "report":
            rep = gold.report(conn, a.set, run_id=a.run, labeller=a.labeller or gold.OWNER)
            print(gold.format_report(rep))
            s.logs.mkdir(parents=True, exist_ok=True)
            out = s.logs / f"gold-report-{rep['set']['id']}-{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
            out.write_text(json.dumps(rep, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            print(f"\nreport: {out}")
        elif a.gold == "import-labels":
            if not a.set or not a.labeller or not a.files:
                raise gold.GoldError("usage: talos enrich gold import-labels --set ID --labeller NAME FILE…")
            res = gold.import_labels(conn, a.set, a.labeller, a.files)
            print(f"imported {res['items']} items ({res['lines']} lines) as {res['labeller']} into answer key"
                  f" {res['set_id']}: {res['new']} new, {res['replaced']} replaced")
            print(f"  fields: {res['fields_set']} sure, {res['fields_unsure']} not sure"
                  + (f" ({', '.join(f'{f} {n}' for f, n in res['unsure'].items())})" if res["unsure"] else "")
                  + f"; {res['notes']} notes")
            print(f"  {res['labeller']} has now labelled {res['labelled_items']} of {res['set_items']} items")
        elif a.gold == "check-sample":
            if not a.set:
                raise gold.GoldError("usage: talos enrich gold check-sample --set ID [--n 25] [--seed N]")
            res = gold.check_sample(conn, a.set, n=a.n or gold.CHECK_N, seed=a.seed,
                                    labeller=a.labeller or "claude", replace=a.replace)
            print(f"check sample for answer key {res['set_id']}: {res['n']} of {res['candidates']} items labelled by"
                  f" {res['labeller']} (seed {res['seed']})")
            print(f"  items: {', '.join(map(str, res['positions']))}")
            if res["excluded"]:
                print(f"  left out, you labelled them yourself: {', '.join(map(str, res['excluded']))}")
            print("  check them in Talos under Operations › Answer key › Check Claude's labels")
        else:
            for r in gold.sets(conn):
                print(f"{r['id']:>4}  {r['name']}  seed {r['seed']}  {r['done']}/{r['items']} done  {r['created_at']:%Y-%m-%d}")
    except gold.GoldError as exc:
        print(f"talos: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def cmd_cases(a, s):
    from talos import cases
    with _conn(s) as conn:
        if a.action == "export":
            run = cases.export(conn, Path(a.path), model=a.model, purpose=a.dimension, dimension=a.dimension,
                               limit=a.limit)
            conn.commit()
            print(f"run {run} → {a.path}")
        elif a.action == "import":
            print(cases.import_decisions(conn, a.run, Path(a.path)))
            conn.commit()
        elif a.action == "accept":
            print(f"accepted {cases.accept(conn, a.run, min_confidence=a.min_confidence)}")
            conn.commit()


def cmd_export(a, s):
    from talos import export
    with _conn(s) as conn:
        if a.ai:
            print(f"{export.ai_rows(conn, Path(a.ai))} threads → {a.ai}")
        else:
            print(export.snapshot(conn, s.exports))


def cmd_web(a, s):
    """Signing in to Talos Web (talos.webauth, docs/security.md). Run by the owner in their own Terminal:
    the password is typed at a hidden prompt and the codes are shown here only, never logged."""
    import getpass
    import json as _json
    import os
    from talos import webauth
    store = webauth.KeychainStore()
    if a.web_cmd == "setup":
        if webauth.is_set_up(store) and input("Signing in is already set up. Replace the password and the "
                                              "authenticator? [y/N] ").strip().lower() != "y":
            return
        while True:
            pw = getpass.getpass(f"New password for Talos Web (at least {webauth.MIN_PASSWORD} characters): ")
            if len(pw) < webauth.MIN_PASSWORD:
                print(f"Too short: at least {webauth.MIN_PASSWORD} characters. A few words in a row work well.")
                continue
            if getpass.getpass("The same password again: ") != pw:
                print("The two did not match. Once more.")
                continue
            break
        secret = webauth.new_totp_secret()
        uri = webauth.otpauth_uri(secret)
        print("\nScan this with your authenticator app (1Password, Google Authenticator, Authy, the iPhone's "
              "Passwords app…):\n")
        import segno
        segno.make(uri, error="m").terminal(compact=True)
        print("\nOr type the key by hand:  " + " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)) + "\n")
        for tries in range(3):
            code = input("Type the 6-digit code your app shows now: ")
            if webauth.totp_step(code, secret) is not None:
                break
            print("That code does not match. Check that the phone's clock is right, and try the next code.")
        else:
            print("Not set up: the codes did not match. Nothing was changed.")
            return
        codes = webauth.new_recovery_codes()
        webauth.setup(store, pw, secret, codes)
        with _conn(s) as conn:
            n = webauth.sign_out(conn, None, everywhere=True)
            webauth.event(conn, "setup", "local:cli", sessions_ended=n)
            conn.commit()
        print("\nRecovery codes: each signs in once instead of a code, if the phone is lost. Save them in your "
              "password manager or on paper now; they are not shown again.\n")
        for c in codes:
            print("    " + c)
        input("\nPress Enter when they are saved.")
        print("Done. Sign in at http://127.0.0.1:7420 (on this Mac) or over Tailscale." + (f" {n} old sessions were ended." if n else ""))
    elif a.web_cmd == "session":
        # A short session for a tool on this Mac (headless checks of the page). The token goes into a
        # file only this user can read, in Playwright's storage-state form, never to the screen.
        with _conn(s) as conn:
            token = webauth.mint(conn, minutes=a.minutes, origin="local:cli")
        state = {"cookies": [{"name": webauth.COOKIE, "value": token, "domain": host, "path": "/", "httpOnly": True,
                              "secure": False, "sameSite": "Strict", "expires": -1}
                             for host in ("127.0.0.1", "localhost")], "origins": []}
        fd = os.open(a.cookie_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            _json.dump(state, f)
        from talos import argus
        argus.Notifier()("Talos: a session was made on this Mac", f"For a tool, valid {a.minutes} minutes.")
        print(f"A {a.minutes}-minute session is in {a.cookie_file} (Playwright storage state; readable by you only).")
    elif a.web_cmd == "sessions":
        with _conn(s) as conn:
            for r in webauth.sessions(conn):
                print(f"{r['id'][:8]}  {r['origin']:<40} last {r['last_seen']:%Y-%m-%d %H:%M}  until {r['expires_at']:%Y-%m-%d %H:%M}")
            print("\nAt the door:")
            for e in webauth.recent_events(conn, 15):
                print(f"  {e['at']:%Y-%m-%d %H:%M}  {e['kind']:<16} {e['origin']}")
    elif a.web_cmd == "signout-all":
        with _conn(s) as conn:
            print(f"{webauth.sign_out(conn, None, everywhere=True)} sessions ended.")


def cmd_serve(a, s):
    import uvicorn
    from talos.web import app
    uvicorn.run(app.create(s), host="127.0.0.1", port=a.port, log_level="warning")


LAUNCHD = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{prefix}.sync</string>
{env}  <key>ProgramArguments</key>
  <array><string>{exe}</string><string>sync</string><string>--then-rules</string></array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{logs}/launchd.out</string>
  <key>StandardErrorPath</key><string>{logs}/launchd.err</string>
</dict>
</plist>
"""


WEB_LAUNCHD = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{prefix}.web</string>
{env}  <key>ProgramArguments</key>
  <array><string>{exe}</string><string>serve</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>{logs}/web.out</string>
  <key>StandardErrorPath</key><string>{logs}/web.err</string>
</dict>
</plist>"""


TEAMS_LAUNCHD = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{prefix}.teams</string>
{env}  <key>ProgramArguments</key>
  <array><string>{exe}</string><string>sync</string><string>teams</string><string>--recent</string><string>20</string></array>
  <key>StartInterval</key><integer>20</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>{logs}/teams-fast.out</string>
  <key>StandardErrorPath</key><string>{logs}/teams-fast.err</string>
</dict>
</plist>
"""


def _launchd_env() -> str:
    """The settings a job needs when they are not the defaults (TALOS_HOME, TALOS_DSN, and TALOS_CONFIG
    when the personal part lives elsewhere, say in a private repository): launchd gives a job none of
    the shell's environment."""
    from xml.sax.saxutils import escape
    env = {k: os.environ[k] for k in ("TALOS_HOME", "TALOS_DSN", "TALOS_CONFIG") if os.environ.get(k)}
    if not env:
        return ""
    pairs = "".join(f"<key>{k}</key><string>{escape(v)}</string>" for k, v in env.items())
    return f"  <key>EnvironmentVariables</key><dict>{pairs}</dict>\n"


def cmd_launchd(a, s):
    exe = Path(sys.argv[0]).resolve()
    name = "web" if a.web else "teams" if a.teams else "sync"
    from talos import personal
    prefix = personal.owner()["service_prefix"]   # owner.json; the labels of services already loaded must not change
    print((WEB_LAUNCHD if a.web else TEAMS_LAUNCHD if a.teams else LAUNCHD).format(exe=exe, logs=s.logs,
                                                                                   env=_launchd_env(), prefix=prefix))
    print(f"<!-- Not installed. To install, save as ~/Library/LaunchAgents/{prefix}.{name}.plist"
          f" and run: launchctl load ~/Library/LaunchAgents/{prefix}.{name}.plist -->")


def cmd_vault(a, s):
    from datetime import datetime

    from talos import obsidian
    root = Path(a.path).expanduser() if a.path else obsidian.DEFAULT_ROOT
    with _conn(s) as conn:
        report = obsidian.import_vault(conn, root, dry_run=a.dry_run)
    print(obsidian.format_report(report))
    s.logs.mkdir(parents=True, exist_ok=True)
    out = s.logs / f"vault-import-{datetime.now().strftime('%Y%m%dT%H%M%S')}{'-dry-run' if a.dry_run else ''}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nreport: {out}")


def _curl_lines(slug: str, port: int = 7420) -> list[str]:
    """The shell lines that check in and report a failure: the token is read from the Keychain at run
    time, curl gives up after 3 seconds, and a failure is swallowed, so the ping can never break the
    script that sends it."""
    base = f"http://127.0.0.1:{port}/argus"
    token = 'ARGUS_TOKEN=$(security find-generic-password -s talos -a argus-checkin-token -w 2>/dev/null || true)'
    auth = '-H "Authorization: Bearer $ARGUS_TOKEN"'
    return [
        token,
        f'curl -fsS -m 3 -X POST {auth} --data-urlencode "expected_next_within=300"'
        f' --data-urlencode "summary=…" {base}/checkin/{slug} >/dev/null 2>&1 || true',
        f'curl -fsS -m 3 -X POST {auth} --data-urlencode "reason=…" {base}/fail/{slug} >/dev/null 2>&1 || true',
    ]


def _ago(t) -> str:
    return t.astimezone().strftime("%Y-%m-%d %H:%M:%S") if t else "—"


def cmd_argus(a, s):
    from talos import argus, secrets
    with db.connect(s.dsn) as conn:
        try:
            if a.action == "register":
                if not a.slug:
                    added = argus.seed(conn)
                    print(f"registered: {', '.join(added) if added else 'nothing new; the day-one services are there'}")
                    return
                if a.probe and a.push:
                    raise argus.ArgusError("a service is --push or --probe JSON, not both")
                row = argus.register(conn, a.slug, a.name, "probe" if a.probe else "push", a.probe, a.grace, a.notes)
                print(f"registered {row['slug']} ({row['kind']}, grace {row['grace_seconds']} s)"
                      + (f": {json.dumps(row['probe'])}" if row["probe"] else ""))
                if row["kind"] == "push":
                    print(f"its check-in lines: talos argus token {row['slug']}")
            elif a.action in ("pause", "resume", "ack") and not a.slug:
                raise argus.ArgusError(f"usage: talos argus {a.action} SLUG")
            elif a.action in ("pause", "resume"):
                argus.set_paused(conn, a.slug, a.action == "pause")
                print(f"{a.slug}: {'paused (shown as paused, never announced)' if a.action == 'pause' else 'watched again'}")
            elif a.action == "ack":
                row = argus.acknowledge(conn, a.slug)
                print(f"{a.slug}: {row['last_summary']}")
            elif a.action == "token":
                slug = argus.check_slug(a.slug or "my-backup")
                have = secrets.exists(argus.TOKEN_KEY)
                if not have:
                    print("No check-in token yet. Make one and store it in the Keychain (paste it at the prompt):\n"
                          "    openssl rand -hex 32\n"
                          f"    security add-generic-password -s talos -a {argus.TOKEN_KEY} -w\n"
                          "Talos web reads it on the first check-in (macOS may ask once to allow it).\n")
                print(f"# {slug}: check in at the end of a good run, report a failure on a bad one.")
                print("# The token is read from the Keychain at run time; it never goes into the script.")
                print("\n".join(_curl_lines(slug)))
            elif a.action == "beat":
                import httpx
                url = secrets.get_optional(argus.BEAT_KEY)
                if not url:
                    print(f"outbound heartbeat: not set up. Store the ping URL with:\n"
                          f"    security add-generic-password -s talos -a {argus.BEAT_KEY} -w")
                    return
                with httpx.Client() as client:
                    res = argus.beat(conn, url, client)
                print(f"outbound heartbeat: {'sent' if res['sent'] else 'FAILED: ' + str(res['error'])}")
                if not res["sent"]:
                    raise SystemExit(1)
            elif a.action == "probe":
                results = argus.probe(conn, argus.ProbeTools(), due_only=False, slugs=[a.slug] if a.slug else None)
                for r in results:
                    print(f"{r['slug']:<22} {'ok  ' if r['ok'] else 'FAIL'}  {r['summary']}")
                if not results:
                    print("nothing probed: no probe service by that name that is not paused")
                # No sweep here: the server's sweep sees the change and is the one that notifies.
            else:
                res = argus.overview(conn, beat_configured=secrets.exists(argus.BEAT_KEY))
                conf = argus.load_settings(s.home)
                for v in res["services"]:
                    print(f"{v['slug']:<22} {v['status']:<8} last {_ago(v['last_checkin_at'])}"
                          f"  next {_ago(v['expected_next_at'])}  {v['last_summary'] or ''}")
                if not res["services"]:
                    print("no services registered; the day-one set: talos argus register")
                b = res["beat"]
                print(f"\noutbound heartbeat: {b['state']}"
                      + (f"; last ok {_ago(b['last_ok_at'])}, failures in a row {b['failures']},"
                         f" total {b['total_ok']} ok / {b['total_failures']} failed" if b["configured"] else
                         f" (store the ping URL: security add-generic-password -s talos -a {argus.BEAT_KEY} -w)")
                      + (f"; last error {b['last_error']} at {_ago(b['last_fail_at'])}" if b["last_error"] else ""))
                print(f"timers in talos serve: {'on' if conf.get('enabled') else 'off'} ({s.home / 'argus.json'})")
        except argus.ArgusError as exc:
            print(f"talos: {exc}", file=sys.stderr)
            raise SystemExit(2) from None


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["doctor"]:   # everything after it is talos-doctor's own (argparse would take its --options)
        cmd_doctor(argparse.Namespace(args=argv[1:]), None)
    p = argparse.ArgumentParser(prog="talos", description="Talos: a local command center over your own mail.")
    p.add_argument("-v", "--verbose", action="store_true")
    from talos import __version__
    p.add_argument("--version", action="version", version=f"talos {__version__} (release notes: CHANGELOG.md)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup").set_defaults(fn=cmd_setup)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    x = sub.add_parser("screen", help="the screen lab: injection samples, talos-doctor's rules and Jev measured on them")
    x.add_argument("screen_cmd", choices=["sources", "source", "import", "rules", "jev", "report"])
    x.add_argument("ids", nargs="*", help="import: the sources (default: those switched on)")
    x.add_argument("--id", help="source: which one")
    x.add_argument("--on", action="store_true")
    x.add_argument("--off", action="store_true")
    x.add_argument("--cap", type=int, help="source: at most this many samples (stratified, the same every time)")
    x.add_argument("--no-cap", action="store_true")
    x.add_argument("--source", action="append", help="rules, jev: only these sources (default: those switched on)")
    x.add_argument("--limit", type=int, help="rules, jev: at most this many samples")
    x.add_argument("--max-usd", type=float, help="jev: the most this run may cost; without it, only the estimate")
    x.add_argument("--run", help="report: this run (default: the latest)")
    x.add_argument("--against", help="report: compare with this run (default: the latest of the other engine)")
    x.set_defaults(fn=cmd_screen)
    sub.add_parser("doctor", help="is this Mac ready for Talos, and the next step (talos-doctor; reads only; "
                                  "talos doctor --help for its options)")
    x = sub.add_parser("where", help="where everything is, and why: the code, your personal part, the data, the database,"
                                     " the Keychain (names only), the services and the backups")
    x.add_argument("--json", action="store_true")
    x.add_argument("--write", metavar="FILE", help="also write it as Markdown to FILE")
    x.set_defaults(fn=cmd_where)
    x = sub.add_parser("config", help="your personal part: init makes it from the examples, never overwriting a file")
    x.add_argument("config_cmd", choices=["init", "path"])
    x.add_argument("--dir", help="init: where (default: TALOS_CONFIG, else TALOS_HOME/config)")
    x.set_defaults(fn=cmd_config)
    x = sub.add_parser("backup", help="back up your own work now (the sync does it nightly); --status: the latest")
    x.add_argument("--status", action="store_true")
    x.add_argument("--keep-days", type=int, default=None, help="prune backups older than this (default 14; the newest 3 stay)")
    x.set_defaults(fn=cmd_backup)
    x = sub.add_parser("auth", help="sign in: graph <account> (Microsoft 365), google [account] (the Google calendar)")
    x.add_argument("provider", choices=["graph", "google"])
    x.add_argument("account", nargs="?", default=None)
    x.set_defaults(fn=cmd_auth)
    x = sub.add_parser("sync")
    x.add_argument("accounts", nargs="*")
    x.add_argument("--limit", type=int)
    x.add_argument("--no-extract", action="store_true", help="defer attachment extraction to 'talos extract'")
    x.add_argument("--recent", type=int, metavar="N",
                   help="teams: the fast lane, only the N chats with the newest messages (no channels)")
    x.add_argument("--channel", metavar="TEAM_ID/CHANNEL_ID", help="teams: only this channel (after a post in it)")
    x.add_argument("--then-rules", action="store_true",
                   help="re-run rules and event extractors afterwards (and the enrich prepass, when mail was added)")
    x.set_defaults(fn=cmd_sync)
    x = sub.add_parser("import")
    x.add_argument("paths", nargs="+")
    x.add_argument("--account", default="local")
    x.add_argument("--folder", default="local")
    x.set_defaults(fn=cmd_import)
    x = sub.add_parser("retry", help="re-ingest messages that failed, from their originals in the vault")
    x.add_argument("--account")
    x.set_defaults(fn=cmd_retry)
    x = sub.add_parser("extract")
    x.add_argument("--limit", type=int)
    x.set_defaults(fn=cmd_extract)
    x = sub.add_parser("reparse", help="re-derive text, snippets and attachment flags after a parser change")
    x.add_argument("--limit", type=int)
    x.set_defaults(fn=cmd_reparse)
    x = sub.add_parser("search")
    x.add_argument("query")
    x.add_argument("--limit", type=int, default=25)
    x.set_defaults(fn=cmd_search)
    x = sub.add_parser("rules")
    x.add_argument("action", choices=["run", "preview", "load", "add", "remove", "removed"])
    x.add_argument("json", nargs="?", help="preview: a JSON condition list; load: a JSON file of rules; remove: a rule id")
    x.add_argument("--all", action="store_true", help="remove: every rule (see --keep)")
    x.add_argument("--keep", help="remove --all: rule ids to keep, comma-separated")
    x.add_argument("--why", help="remove: why, kept with the removed rule")
    x.set_defaults(fn=cmd_rules)
    x = sub.add_parser("events")
    x.add_argument("action", choices=["run"])
    x.set_defaults(fn=cmd_events)
    x = sub.add_parser("calendar", help="the calendar: copy the Microsoft 365, iCloud and Google calendars, or list them")
    x.add_argument("action", nargs="?", default="list", choices=["sync", "list"])
    x.set_defaults(fn=cmd_calendar)
    x = sub.add_parser("importance", help="score messages by importance; manage VIP senders")
    x.add_argument("action", nargs="?", default="run", choices=["run", "vip-add", "vip-remove", "vip-list"])
    x.add_argument("value", nargs="?", help="vip-add/vip-remove: an address or a domain")
    x.add_argument("--since", type=int, metavar="DAYS", help="only messages (and threads) of the last DAYS days")
    x.set_defaults(fn=cmd_importance)
    x = sub.add_parser("changeset")
    x.add_argument("action", choices=["create", "list", "plan", "show", "dry-run", "commit", "apply", "check", "reconcile",
                                      "retry", "undo", "cancel"])
    x.add_argument("id", nargs="?", type=int)
    x.add_argument("--title")
    x.add_argument("--op")
    x.add_argument("--args")
    x.add_argument("--where", help="JSON condition list")
    x.add_argument("--ids", help="create: message ids, comma-separated, instead of --where")
    x.add_argument("--sample", type=int, metavar="N", help="check: read back only N applied messages, at random")
    x.add_argument("--ids-file", metavar="FILE", help="create: message ids from a file (one per line, or commas)")
    x.add_argument("--max", type=int, default=1, metavar="N",
                   help="commit: refuse if the changeset would change more than N messages (default 1)")
    x.set_defaults(fn=cmd_changeset)
    x = sub.add_parser("structure", help="the mailbox structure planner: plan, report, why, changesets (never committed)")
    x.add_argument("action", choices=["plan", "report", "why", "changesets"])
    x.add_argument("id", nargs="?", type=int, help="why: a message id")
    x.add_argument("--account")
    x.add_argument("--rules", help="the rules file (default: rules/structure.json in the repo)")
    x.add_argument("--dry-run", action="store_true", help="plan: compute and report without storing (read-only)")
    x.add_argument("--target", help="changesets: one target label, e.g. 'Talos/Keep/Receipts & invoices'")
    x.add_argument("--limit", type=int, metavar="N", help="changesets: at most N messages per changeset (newest first)")
    x.add_argument("--new-since", metavar="WHEN", help="changesets: only mail new or re-placed since WHEN (2026-09-25, 1d, 12h)")
    x.add_argument("--archive-unlabelled", action="store_true",
                   help="changesets: archive also messages whose Talos label is not on the server yet")
    x.set_defaults(fn=cmd_structure)
    x = sub.add_parser("taxonomy", help="load the closed value lists into the dimensions")
    x.add_argument("action", choices=["load"])
    x.add_argument("path", nargs="?", help="the taxonomy file (default: TALOS_HOME/config/taxonomy.json, else rules/taxonomy.json in the repo)")
    x.set_defaults(fn=cmd_taxonomy)
    x = sub.add_parser("discovery", help="the discovery drafts: load them for review, see progress, export decisions")
    x.add_argument("action", choices=["load", "status", "export"])
    x.add_argument("dir", nargs="?", help="the folder with systems.json, candidates.json and my-setup.json"
                                          " (default: TALOS_HOME/config/discovery)")
    x.add_argument("--out", help="export: write the files here instead of over the drafts")
    x.set_defaults(fn=cmd_discovery)
    x = sub.add_parser("enrich", help="the deterministic enrichment pre-pass (step A), its coverage report,"
                                      " the answer key (step B) and Jev on it (step C)")
    x.add_argument("action", choices=["prepass", "report", "gold", "jev", "accept", "unaccept", "propagate"])
    x.add_argument("gold", nargs="?", choices=["sample", "sample-uncertain", "report", "list", "import-labels",
                                               "check-sample", "archive", "propagate-groups", "gold",
                                               "backfill", "backfill-report", "focus", "new"],
                   default="list", help="gold: sample a new answer key, report on one, list them, import another"
                                        " labeller's answers, or draw the sample you check")
    x.add_argument("files", nargs="*", help="gold import-labels: JSONL files, one item per line")
    x.add_argument("--dry-run", action="store_true",
                   help="prepass: do everything, report the counts, roll back; gold sample: draw, store nothing;"
                        " jev gold and backfill: send nothing; accept, unaccept: show the change, roll back")
    x.add_argument("--n", type=int, help="gold sample: how many items (default 300); check-sample: (default 25)")
    x.add_argument("--seed", type=int, help="gold sample: the seed (default: one more than the highest used)")
    x.add_argument("--name", help="gold sample: a name for the set")
    x.add_argument("--set", type=int, help="gold report: the answer key (default: the newest); import-labels and"
                                           " check-sample: required")
    x.add_argument("--labeller", help="gold import-labels: whose answers (claude); report: the reference"
                                      " (default: the owner); check-sample: whose answers you check (default claude)")
    x.add_argument("--replace", action="store_true", help="gold check-sample: draw a new check sample")
    x.add_argument("--run", help="gold report: also score this model run's proposals; jev gold and backfill: resume"
                                 " this run; jev report, backfill-report, accept, unaccept: the run")
    x.add_argument("--unit", choices=["message", "context"], help="jev gold: the unit design of the records")
    x.add_argument("--template", type=int, choices=[1, 2],
                   help="jev gold: the question templates (default 2: ask and route as one choice, a Teams set)")
    x.add_argument("--limit", type=int, help="jev gold: only the first N items of the answer key; jev backfill: the"
                                             " first N units")
    x.add_argument("--max-cases", type=int, help="jev gold: refuse to send more cases than this (default 400);"
                                                 " jev backfill: (default 60000)")
    x.add_argument("--concurrency", type=int, help="jev gold and backfill: requests in flight (default 8)")
    x.add_argument("--compare", help="jev report: a second run, side by side")
    x.add_argument("--stage", choices=["machine", "person", "teams"], help="jev backfill: which units; jev focus: only"
                                                                           " this stage's units")
    x.add_argument("--field", help="jev focus: the field to ask (sender_kind, kind, origin, type, topic, value, ask,"
                                   " route), or several separated by commas, asked in one combined run (keep: value's"
                                   " cases whose keep side is unsure)")
    x.add_argument("--where", choices=["uncertain", "disagree", "all"],
                   help="jev focus: under its two-level threshold, against a rule or pre-pass value, or every case")
    x.add_argument("--gold", dest="focus_gold", action="store_true",
                   help="jev focus: ask the answer key (--set, default 1) and report its accuracy, before the archive")
    x.add_argument("--since", type=int, help="jev backfill: only units with a message in the last DAYS days")
    x.add_argument("--budget", type=float, help="jev backfill: the most this invocation may spend, in dollars")
    for f, d in (("origin", 0.85), ("type", 0.90), ("topic", 0.85), ("value", 0.85), ("route", None), ("ask", None),
                 ("sender_kind", None), ("sphere", None), ("form", None), ("keep", None), ("kind", None)):
        x.add_argument(f"--{f.replace('_', '-')}", dest=f"accept_{f}", type=float,
                       help=f"accept: the threshold for {f}" + (f" (default {d})" if d else " (default: not accepted)"))
    x.add_argument("--margin", type=float, help="accept: the margin every exact field needs (default 0.15)")
    x.add_argument("--two-level", action="store_true",
                   help="accept: the two-level preset: sender_kind, sphere, form and keep at 0.90, kind at 0.85,"
                        " origin, type,"
                        " topic and value at 0.70, an exact value only on its boundary's side")
    x.add_argument("--fields", help="propagate: the fields to write (comma-separated, or boundaries);"
                                    " gold sample-uncertain: the fields the set labels (default the six)")
    x.add_argument("--undo", action="store_true", help="gold archive: put the set back on the screen")
    x.add_argument("--apply", action="store_true", help="gold propagate-groups: write (without it, a dry run)")
    x.add_argument("--full", action="store_true", help="prepass: recompute every subject pattern key")
    x.add_argument("--backlog", action="store_true",
                   help="jev new: every unjudged message, however old (default: received in the last 7 days)")
    x.add_argument("--status", action="store_true", help="jev new: enrich.json, today's spend and the last runs")
    x.set_defaults(fn=cmd_enrich)
    x = sub.add_parser("cases")
    x.add_argument("action", choices=["export", "import", "accept"])
    x.add_argument("--path")
    x.add_argument("--run")
    x.add_argument("--model", default="jev")
    x.add_argument("--dimension", default="topic")
    x.add_argument("--limit", type=int)
    x.add_argument("--min-confidence", type=float, default=0.0)
    x.set_defaults(fn=cmd_cases)
    x = sub.add_parser("export")
    x.add_argument("--ai", metavar="FILE")
    x.set_defaults(fn=cmd_export)
    x = sub.add_parser("web", help="signing in to Talos Web: setup (password and authenticator), session, sessions, signout-all")
    x.add_argument("web_cmd", choices=["setup", "session", "sessions", "signout-all"])
    x.add_argument("--minutes", type=int, default=30, help="session: how long it lasts (at most 120)")
    x.add_argument("--cookie-file", default="talos-session.json", help="session: where the storage state is written")
    x.set_defaults(fn=cmd_web)
    x = sub.add_parser("serve")
    x.add_argument("--port", type=int, default=7420)
    x.set_defaults(fn=cmd_serve)
    x = sub.add_parser("launchd")
    x.add_argument("--web", action="store_true", help="the web server (kept alive) instead of the 5-minute sync")
    x.add_argument("--teams", action="store_true", help="the Teams fast lane (every 20 seconds) instead of the 5-minute sync")
    x.set_defaults(fn=cmd_launchd)
    x = sub.add_parser("vault", help="the Obsidian vault Talos: lift it into Talos Web")
    x.add_argument("action", choices=["import"])
    x.add_argument("path", nargs="?", help="the vault folder (default: the iCloud vault Talos)")
    x.add_argument("--dry-run", action="store_true", help="do everything, then roll it back; print the report")
    x.set_defaults(fn=cmd_vault)
    x = sub.add_parser("argus", help="the service monitor: status, register, pause, resume, ack, token, beat, probe")
    x.add_argument("action", nargs="?", default="status",
                   choices=["status", "register", "pause", "resume", "ack", "token", "beat", "probe"])
    x.add_argument("slug", nargs="?")
    x.add_argument("--name")
    x.add_argument("--push", action="store_true", help="register: the service checks in (the default)")
    x.add_argument("--probe", metavar="JSON", help='register: Argus checks it, e.g. \'{"type": "http", "url":'
                                                   ' "http://127.0.0.1:8888/health"}\'')
    x.add_argument("--grace", type=int, metavar="SECONDS", help="register: late after deadline + grace, down after"
                                                                " twice the grace (default 300; probes 2 × every)")
    x.add_argument("--notes")
    x.set_defaults(fn=cmd_argus)

    a = p.parse_args(argv)
    s = config.load()
    _logging(s, a.verbose)
    a.fn(a, s)


if __name__ == "__main__":
    main()
