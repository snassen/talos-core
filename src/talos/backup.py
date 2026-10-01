"""The nightly backup of what only the owner made: `talos backup`, and once a day from the sync.

Most of the database can be made again: the vault holds every original, a sync fetches what is new,
and rules, events and Jev's answers can be run again. What cannot is the owner's own work: binders,
work items and notes, rules written by hand, answer keys, Studio decisions, the history of every
changeset (with the inverses undo needs), calendars of Talos's own, drafts and settings, and every
value the owner set. Time Machine copies the whole database folder; this is a second copy, readable
and small, in TALOS_HOME/backups/<date>/:

- own.dump: those tables in full, schema and data (pg_dump -Fc; restore with pg_restore);
- own-values.jsonl.gz: every value the owner set (human assignments), and the ones a Studio lift
  accepted, each with a stable key for its message (the vault's sha256 and the Message-ID), so they
  can be given back even to a database rebuilt from the vault, where every id differs;
- own-links.jsonl.gz: the links the owner made or imported (binder memberships, a work item's mail),
  keyed the same way;
- manifest.json: when, the Talos version, the migrations, and the count of every part.

The newest KEEP_DAYS days are kept (never fewer than KEEP_MIN). It runs from `talos sync --then-rules`
once a day after HOUR (due()), and checks in with Argus as talos-backup. docs/backup.md says how to
restore. It reads only; the dump is a consistent snapshot and never blocks the sync.
"""

from __future__ import annotations

import gzip
import json
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import psycopg
from psycopg import conninfo
from psycopg.rows import dict_row

from talos import __version__, config

SLUG = "talos-backup"
KEEP_DAYS = 14
KEEP_MIN = 3
HOUR = 2                      # the first sync after 02:00 local time makes the day's backup
EVERY = 26 * 3600             # Argus: seconds until the next check-in, a day and a bit
# Tables that hold only the owner's own work, dumped whole (entity: the ids those rows hang on).
OWN_TABLES = (
    "entity", "object", "note", "work_item", "work_item_event", "rule", "rule_removed", "dimension",
    "gold_set", "gold_item", "gold_label", "gold_group_message", "gold_group_case", "gold_check_item",
    "studio_decision", "studio_verdict", "studio_lift", "discovery_source", "discovery_item",
    "watcher", "aggregation", "improvement_job", "importance_vip", "calendar", "calendar_entry",
    "draft", "signature", "signature_default", "send_setting", "send_log", "changeset", "changeset_op",
    "account", "my_address", "web_state", "argus_service", "model_run", "jev_case", "jev_prediction",
    "schema_migration",
)
# A message's stable key, for the values and links that point at one (or at its thread).
_KEY = """jsonb_build_object('entity', e.id, 'kind', e.kind,
            'raw_sha256', coalesce(m.raw_sha256, tm.raw_sha256), 'message_id', coalesce(m.rfc_message_id, tm.rfc_message_id))"""
VALUES_SQL = f"""
    select a.dimension_id, a.value, a.source_kind, a.source_ref, a.status, a.confidence, a.decided_by,
           a.created_at, {_KEY} as target
    from assignment a join entity e on e.id = a.entity_id
    left join message m on m.id = e.id
    left join lateral (select x.raw_sha256, x.rfc_message_id from message x where x.thread_id = e.id
                       order by x.received_at nulls last, x.id limit 1) tm on e.kind = 'thread'
    where a.status = 'active' and (a.source_kind = 'human' or a.decided_by like 'studio-lift:%')
    order by a.id"""
LINKS_SQL = f"""
    select g.rel, g.source, g.src, g.dst, s.kind as src_kind, d.kind as dst_kind,
           jsonb_build_object('raw_sha256', ms.raw_sha256, 'message_id', ms.rfc_message_id) as src_message,
           jsonb_build_object('raw_sha256', md.raw_sha256, 'message_id', md.rfc_message_id) as dst_message
    from edge g join entity s on s.id = g.src join entity d on d.id = g.dst
    left join message ms on ms.id = g.src left join message md on md.id = g.dst
    where g.source <> 'ingest' and g.source not like 'extractor:%'
    order by g.src, g.dst"""


class BackupError(RuntimeError):
    pass


def folder(s: config.Settings) -> Path:
    return s.home / "backups"


def pg_dump() -> str:
    """The pg_dump that matches the server (Homebrew's PostgreSQL 18 first, else whatever is on PATH)."""
    for p in ("/opt/homebrew/opt/postgresql@18/bin/pg_dump", "/usr/local/opt/postgresql@18/bin/pg_dump"):
        if Path(p).exists():
            return p
    found = shutil.which("pg_dump")
    if not found:
        raise BackupError("pg_dump not found: install PostgreSQL's client tools")
    return found


def _jsonl_gz(conn: psycopg.Connection, sql: str, path: Path) -> int:
    n = 0
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for row in conn.execute(sql):
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            n += 1
    return n


def run(s: config.Settings, *, keep_days: int = KEEP_DAYS, now: datetime | None = None) -> dict:
    """Make today's backup (replacing one already made today), then prune old ones. Returns the manifest.
    It is written to a hidden .partial folder first, so a failed run leaves nothing that looks like a backup."""
    now = (now or datetime.now()).astimezone()
    t0 = time.monotonic()
    day = folder(s) / now.strftime("%Y-%m-%d")
    tmp = day.with_name(f".{day.name}.partial")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    tmp.chmod(0o700)
    try:
        manifest = _write(s, tmp, now)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    manifest["seconds"] = round(time.monotonic() - t0, 1)
    (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (tmp / "manifest.json").chmod(0o600)
    shutil.rmtree(day, ignore_errors=True)
    tmp.rename(day)
    manifest["pruned"] = prune(s, keep_days=keep_days, now=now)
    manifest["path"] = str(day)
    return manifest


def _write(s: config.Settings, tmp: Path, now: datetime) -> dict:
    """The dump, the values and the links into tmp; returns the manifest (without its timing)."""
    info = conninfo.conninfo_to_dict(s.dsn)
    args = [pg_dump(), "-Fc", "-Z", "6", "-f", str(tmp / "own.dump")]
    for k, flag in (("host", "-h"), ("port", "-p"), ("user", "-U"), ("dbname", "-d")):
        if info.get(k):
            args += [flag, str(info[k])]
    for t in OWN_TABLES:
        args += ["-t", t]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError(f"pg_dump did not run: {exc}") from exc
    if r.returncode != 0:
        raise BackupError(f"pg_dump failed: {r.stderr.strip()[:400]}")
    with psycopg.connect(s.dsn, row_factory=dict_row) as c:
        c.isolation_level, c.read_only = psycopg.IsolationLevel.REPEATABLE_READ, True   # one snapshot for all parts
        values = _jsonl_gz(c, VALUES_SQL, tmp / "own-values.jsonl.gz")
        links = _jsonl_gz(c, LINKS_SQL, tmp / "own-links.jsonl.gz")
        counts = {t: c.execute(f"select count(*) as n from {t}").fetchone()["n"] for t in OWN_TABLES}
        migrations = [r["name"] for r in c.execute("select name from schema_migration order by name")]
    files = {}
    for f in tmp.iterdir():
        f.chmod(0o600)
        files[f.name] = f.stat().st_size
    return {"made": now.isoformat(timespec="seconds"), "talos": __version__, "migrations": migrations,
            "tables": counts, "values": values, "links": links, "files": files,
            "restore": "docs/backup.md: pg_restore own.dump into a database at these migrations; the values and"
                       " links by their raw_sha256 into one rebuilt from the vault"}


def backups(s: config.Settings) -> list[Path]:
    """The finished backups, newest first (a folder per day, with its manifest)."""
    root = folder(s)
    if not root.exists():
        return []
    return sorted((p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
                   and (p / "manifest.json").exists()), reverse=True)


def prune(s: config.Settings, *, keep_days: int = KEEP_DAYS, now: datetime | None = None) -> list[str]:
    """Remove backups older than keep_days, but never the newest KEEP_MIN."""
    now = (now or datetime.now()).astimezone()
    cutoff = (now - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    gone = []
    for p in backups(s)[KEEP_MIN:]:
        if p.name < cutoff:
            shutil.rmtree(p)
            gone.append(p.name)
    return gone


def latest(s: config.Settings) -> dict | None:
    b = backups(s)
    if not b:
        return None
    return {**json.loads((b[0] / "manifest.json").read_text(encoding="utf-8")), "path": str(b[0])}


def due(s: config.Settings, now: datetime | None = None) -> bool:
    """Is today's backup still to be made? (After HOUR, and none made today yet.)"""
    now = (now or datetime.now()).astimezone()
    if now.hour < HOUR:
        return False
    b = backups(s)
    return not b or b[0].name < now.strftime("%Y-%m-%d")
