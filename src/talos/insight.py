"""Computed answers kept in the database (table insight_cache), so a page never scans the archive
on every load.

An answer is stored with a fingerprint of what it was computed from (cheap counters: the newest
ids, the tables' write counts from pg_stat, the accept stamps). read() gives the stored answer and
whether it is stale; a stale answer is shown as it is while refresh_behind() computes it again in a
thread of its own (one at a time per answer), so a page load stays an index lookup. A missing
answer is computed once, in the request that first asks for it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Callable

import psycopg
from psycopg.types.json import Jsonb

from talos import db

log = logging.getLogger("talos.insight")

_RUNNING: set[str] = set()
_LOCK = threading.Lock()


def table_writes(conn: psycopg.Connection, tables: list[str]) -> int:
    """Rows inserted, updated and deleted in the tables since the statistics were reset: moves on
    any write (a little after it is committed), and costs one catalog lookup."""
    return conn.execute("select coalesce(sum(n_tup_ins + n_tup_upd + n_tup_del), 0)::bigint as n"
                        " from pg_stat_user_tables where relname = any(%s)", (tables,)).fetchone()["n"]


def fingerprint(parts) -> str:
    return hashlib.sha256(json.dumps(parts, default=str, sort_keys=True).encode()).hexdigest()[:24]


def read(conn: psycopg.Connection, name: str) -> dict | None:
    return conn.execute("select name, fingerprint, computed_at, seconds, payload from insight_cache where name = %s",
                        (name,)).fetchone()


def store(conn: psycopg.Connection, name: str, fp: str, payload: dict, seconds: float) -> dict:
    row = conn.execute(
        "insert into insight_cache (name, fingerprint, payload, seconds) values (%s, %s, %s, %s)"
        " on conflict (name) do update set fingerprint = excluded.fingerprint, payload = excluded.payload,"
        " seconds = excluded.seconds, computed_at = now()"
        " returning name, fingerprint, computed_at, seconds, payload",
        (name, fp, Jsonb(json.loads(json.dumps(payload, default=str))), seconds)).fetchone()
    return row


def compute(conn: psycopg.Connection, name: str, fp_fn: Callable, fn: Callable) -> dict:
    """Compute and store an answer now (the fingerprint is taken first, so a write during the
    computation makes the answer stale rather than wrongly fresh)."""
    fp = fp_fn(conn)
    t0 = time.monotonic()
    payload = fn(conn)
    row = store(conn, name, fp, payload, round(time.monotonic() - t0, 2))
    conn.commit()
    return row


def refresh_behind(dsn: str, name: str, fp_fn: Callable, fn: Callable) -> bool:
    """Compute the answer again in a thread of its own; False when one is already running."""
    with _LOCK:
        if name in _RUNNING:
            return False
        _RUNNING.add(name)

    def work():
        try:
            with db.connect(dsn) as c:
                compute(c, name, fp_fn, fn)
        except Exception:
            log.exception("insight: computing %s failed", name)
        finally:
            with _LOCK:
                _RUNNING.discard(name)

    threading.Thread(target=work, name=f"insight-{name}", daemon=True).start()
    return True


def running(name: str) -> bool:
    with _LOCK:
        return name in _RUNNING


def get(conn: psycopg.Connection, name: str, fp_fn: Callable, fn: Callable, *, dsn: str | None = None,
        refresh: bool = False, first_behind: bool = False) -> dict:
    """The answer for a page: stored and fresh; stored and stale (recomputed behind it when dsn is
    given, else now); or computed now when there is none, or when refresh asks for it. first_behind
    (with dsn): a missing answer is computed behind the page too, and payload is None meanwhile."""
    row = read(conn, name)
    if row is None and first_behind and dsn and not refresh:
        refresh_behind(dsn, name, fp_fn, fn)
        return {"name": name, "fingerprint": None, "computed_at": None, "seconds": None, "payload": None,
                "stale": True, "refreshing": True}
    if row is None or refresh or (row["fingerprint"] != fp_fn(conn) and dsn is None):
        row = compute(conn, name, fp_fn, fn)
        return {**row, "stale": False, "refreshing": False}
    stale = row["fingerprint"] != fp_fn(conn)
    if stale:
        refresh_behind(dsn, name, fp_fn, fn)
    return {**row, "stale": stale, "refreshing": stale or running(name)}


def get_own(conn: psycopg.Connection, name: str, own_fp: Callable, data_fp: Callable, fn: Callable, *,
            dsn: str | None = None, refresh: bool = False) -> dict:
    """get() for a page that also shows what the owner set themselves (the aggregations they saved, say).
    Two fingerprints: own_fp, exact (read from the rows, never from pg_stat, which lags a commit a little),
    for what the owner changes; a change there computes the answer now, so the page shows the change at once.
    data_fp for what arrives on its own (new mail, new values): a change there shows the stored answer and computes it again behind the page."""
    fp_fn = lambda c: f"{own_fp(c)}.{data_fp(c)}"
    row = read(conn, name)
    if row is not None and not refresh and str(row["fingerprint"]).split(".")[0] != own_fp(conn):
        return {**compute(conn, name, fp_fn, fn), "stale": False, "refreshing": False}
    return get(conn, name, fp_fn, fn, dsn=dsn, refresh=refresh)


def iso(o):
    """For a payload with datetimes: stored as ISO strings, which every browser's Date reads (str() writes a space)."""
    return o.isoformat() if hasattr(o, "isoformat") else str(o)
