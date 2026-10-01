"""The sources' switches and imports (talos screen sources | source | import)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from talos.screenlab import corpus
from talos.screenlab.sources import SOURCES, Fetcher, SourceError, capped


def seed(conn: psycopg.Connection) -> None:
    """Every known source gets a row (off, with its default cap); a row once made keeps the owner's settings."""
    for s in SOURCES.values():
        conn.execute("insert into screen_source (id, title, license, url, cap) values (%s, %s, %s, %s, %s)"
                     " on conflict (id) do update set title = excluded.title, license = excluded.license, url = excluded.url",
                     (s.id, s.title, s.license, s.url, s.default_cap))


def listing(conn: psycopg.Connection) -> list[dict]:
    seed(conn)
    return conn.execute("select * from screen_source order by id").fetchall()


def set_source(conn: psycopg.Connection, source_id: str, *, enabled: bool | None = None, cap: int | None = None,
               no_cap: bool = False) -> dict:
    seed(conn)
    if source_id not in SOURCES:
        raise SourceError(f"no source {source_id!r}; talos screen sources lists them")
    if enabled is not None:
        conn.execute("update screen_source set enabled = %s where id = %s", (enabled, source_id))
    if cap is not None or no_cap:
        conn.execute("update screen_source set cap = %s where id = %s", (None if no_cap else cap, source_id))
    return conn.execute("select * from screen_source where id = %s", (source_id,)).fetchone()


def import_source(conn: psycopg.Connection, source_id: str, cache: Path, fetcher: Fetcher | None = None) -> dict:
    """Fetch (once, into the cache), cap, deduplicate against everything stored, and store. Counts only."""
    seed(conn)
    src = SOURCES.get(source_id)
    if not src:
        raise SourceError(f"no source {source_id!r}")
    row = conn.execute("select cap from screen_source where id = %s", (source_id,)).fetchone()
    f = fetcher or Fetcher(cache, source_id)
    samples = capped(list(src.load(f)), row["cap"])
    counts = corpus.store(conn, source_id, samples)
    conn.execute("update screen_source set revision = %s, imported_at = %s, counts = %s where id = %s",
                 (", ".join(f.revisions)[:500], datetime.now(timezone.utc), Jsonb(counts), source_id))
    return counts
