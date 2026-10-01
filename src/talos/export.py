"""Snapshots for analysis and for AI: Parquet for DuckDB, JSONL per thread for models.

Exports are read-only copies taken at one moment, written to a timestamped folder
under ~/TalosData/exports. Heavy crunching runs on those files with DuckDB and never
loads the live database.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq

TABLES = {
    "messages": """
        select m.id, m.account_id, m.thread_id, m.received_at, m.sent_at, m.direction, m.from_address,
               split_part(m.from_address, '@', 2) as from_domain, m.from_name, m.subject, m.is_automated,
               m.has_attachments, m.size_bytes, m.list_id,
               (select array_agg(distinct x) from message_location l, unnest(l.labels) x
                 where l.message_id = m.id and l.present) as labels,
               (select array_agg(distinct coalesce(l.folder_path, l.folder)) from message_location l
                 where l.message_id = m.id and l.present) as folders
        from message m""",
    "participants": "select message_id, role, address, name from participant",
    "attachments": "select id, message_id, blob_sha256, filename, content_type, size_bytes, extract_status from attachment",
    "assignments": "select entity_id, dimension_id, value, source_kind, source_ref, confidence from effective_assignment",
    # Each message's values, counting its thread's (a human decision on the thread beats a rule).
    "message_values": "select message_id, dimension_id, value, source_kind, source_ref, confidence, via"
                      " from effective_message_assignment",
    "edges": "select src, rel, dst, source from edge",
    "events": "select id, message_id, extractor, system, kind, status, occurred_at from event",
    "people": "select p.id, p.display_name, p.primary_address, p.is_me, o.domain as org_domain"
              " from person p left join org o on o.id = p.org_id",
}


def snapshot(conn: psycopg.Connection, root: Path) -> Path:
    out = Path(root) / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    out.mkdir(parents=True, exist_ok=False)
    for name, query in TABLES.items():
        rows = conn.execute(query).fetchall()
        table = pa.Table.from_pylist(rows) if rows else pa.table({})
        pq.write_table(table, out / f"{name}.parquet")
    return out


def ai_rows(conn: psycopg.Connection, path: Path, *, since: datetime | None = None, max_chars: int = 8000) -> int:
    """One JSON line per thread: people, subject, assignments and the new text of each message."""
    params: list = []
    where = ""
    if since:
        where = "where t.last_at >= %s"
        params.append(since)
    threads = conn.execute(f"select t.id, t.subject, t.first_at, t.last_at from thread t {where} order by t.id",
                           params).fetchall()
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for t in threads:
            msgs = conn.execute(
                "select m.received_at, m.direction, m.from_address, coalesce(x.quote_stripped, '') text"
                " from message m left join message_text x on x.message_id = m.id where m.thread_id = %s"
                " order by m.received_at nulls first", (t["id"],)).fetchall()
            values = {}
            for r in conn.execute("select dimension_id, value from effective_assignment where entity_id = %s",
                                  (t["id"],)):
                values.setdefault(r["dimension_id"], []).append(r["value"])
            budget, parts = max_chars, []
            for m in msgs:
                piece = m["text"][:budget]
                budget -= len(piece)
                parts.append({"at": m["received_at"].isoformat() if m["received_at"] else None,
                              "direction": m["direction"], "from": m["from_address"], "text": piece})
                if budget <= 0:
                    break
            f.write(json.dumps({"thread_id": t["id"], "subject": t["subject"], "values": values,
                                "messages": parts}, ensure_ascii=False) + "\n")
            n += 1
    return n
