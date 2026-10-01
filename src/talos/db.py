"""Connections and migrations.

Migrations are plain SQL files in talos/sql, applied in name order, each in its
own transaction and recorded in schema_migration. There is no down-migration:
the vault holds the originals, so a broken database is rebuilt, not repaired.
"""

from __future__ import annotations

from importlib import resources

import psycopg
from psycopg import conninfo, sql
from psycopg.rows import dict_row


def connect(dsn: str, *, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(dsn, autocommit=autocommit, row_factory=dict_row)


def migrations() -> list[tuple[str, str]]:
    files = resources.files("talos") / "sql"
    return sorted((f.name, f.read_text(encoding="utf-8")) for f in files.iterdir() if f.name.endswith(".sql"))


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply every migration not yet recorded. Returns the names applied."""
    with conn.transaction():
        conn.execute(
            "create table if not exists schema_migration ("
            " name text primary key, applied_at timestamptz not null default now())"
        )
    done = {r["name"] for r in conn.execute("select name from schema_migration")}
    applied = []
    for name, text in migrations():
        if name in done:
            continue
        with conn.transaction():
            conn.execute(text)
            conn.execute("insert into schema_migration (name) values (%s)", (name,))
        applied.append(name)
    return applied


def ensure_database(dsn: str) -> bool:
    """Create the database named in the DSN if it does not exist. Returns True if created."""
    name = conninfo.conninfo_to_dict(dsn).get("dbname") or "talos"
    admin = conninfo.make_conninfo(dsn, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        exists = conn.execute("select 1 from pg_database where datname = %s", (name,)).fetchone()
        if exists:
            return False
        conn.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
        return True


def drop_database(dsn: str) -> None:
    """Drop the database named in the DSN. Used by the test suite only."""
    name = conninfo.conninfo_to_dict(dsn).get("dbname")
    if not name or name == "talos":
        raise ValueError("refusing to drop the main talos database")
    admin = conninfo.make_conninfo(dsn, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))
