"""Re-deriving message text from the vault after the parser changes.

Every message row records the PARSER_VERSION that produced it. When the parser improves,
`talos reparse` reads each older message's original from the vault, parses it again and
refreshes what the parser derives:

- message_text: body_text, quote_stripped and the search vector
- message: snippet, has_attachments and parser_version (and a subject or sender name
  that was stored with U+FFFD, if the new parse can read it)
- message: the stored rule headers and the automated reasons (headers -> 'automated'),
  with is_automated, and the subject pattern (message_pattern)
- attachment rows for parts that were not found before (matched on part_path)

People, participants, threads, edges and existing attachment rows are left alone:
they carry ids that assignments, objects and changesets point at. Work goes in batches,
each committed on its own, so an interrupted run loses at most one batch and simply
continues where it stopped the next time.
"""

from __future__ import annotations

import logging

import psycopg
from psycopg.types.json import Jsonb

from talos import mime
from talos.ingest import MAX_SEARCH_BODY, SEARCH_SQL, Ingestor
from talos.vault import Vault

log = logging.getLogger("talos.reparse")


def outdated(conn: psycopg.Connection) -> int:
    # E-mail only: a Teams row's parser_version is teams_ingest.MAPPER_VERSION, and its original is JSON.
    return conn.execute("select count(*) n from message where medium = 'email' and parser_version < %s"
                        " and raw_sha256 is not null", (mime.PARSER_VERSION,)).fetchone()["n"]


def reparse(conn: psycopg.Connection, vault: Vault, *, limit: int | None = None, batch: int = 200) -> dict:
    """Re-parse up to `limit` messages older than mime.PARSER_VERSION. Returns counts."""
    ing = Ingestor(conn, vault)
    done = failed = 0
    last_id = 0
    while limit is None or done + failed < limit:
        size = batch if limit is None else min(batch, limit - done - failed)
        with conn.transaction():  # one batch, committed on its own
            rows = conn.execute(
                "select id, raw_sha256, subject, from_name from message"
                " where medium = 'email' and parser_version < %s and raw_sha256 is not null and id > %s"
                " order by id limit %s",
                (mime.PARSER_VERSION, last_id, size)).fetchall()
            if not rows:
                break
            for r in rows:
                last_id = r["id"]
                try:
                    with conn.transaction():  # a savepoint: one bad message does not undo the batch
                        _one(conn, ing, vault, r)
                    done += 1
                except Exception as exc:
                    ing.reset_caches()
                    failed += 1
                    log.warning("reparse failed message=%s error=%s: %s", r["id"], type(exc).__name__, exc)
        log.info("reparsed %s messages (%s failed) up to id %s", done, failed, last_id)
    return {"reparsed": done, "failed": failed, "remaining": outdated(conn)}


def _one(conn: psycopg.Connection, ing: Ingestor, vault: Vault, row: dict) -> None:
    mid = row["id"]
    p = mime.parse(vault.get(row["raw_sha256"], "raw"))

    # The same search document ingest builds (Ingestor._message).
    people = []
    for addrs in ([p.from_] if p.from_ else [], [p.sender] if p.sender else [], p.reply_to, p.to, p.cc, p.bcc):
        people += [f"{a.name or ''} {a.address}" for a in addrs]
    conn.execute(
        f"insert into message_text (message_id, body_kind, body_text, quote_stripped, search)"
        f" values (%(id)s, %(kind)s, %(text)s, %(stripped)s, {SEARCH_SQL})"
        f" on conflict (message_id) do update set body_kind = excluded.body_kind, body_text = excluded.body_text,"
        f" quote_stripped = excluded.quote_stripped, search = excluded.search",
        {"id": mid, "kind": p.body_kind, "text": p.body_text, "stripped": p.quote_stripped,
         "subject": p.subject, "people": " ".join(people),
         "filenames": " ".join(part.filename or "" for part in p.attachments),
         "body": (p.quote_stripped or p.body_text)[:MAX_SEARCH_BODY]})

    known = {r["part_path"] for r in conn.execute("select part_path from attachment where message_id = %s", (mid,))}
    for part in p.attachments:
        if part.part_path not in known:
            ing._attachment(mid, part)

    def repaired(old: str | None, new: str | None) -> str | None:
        return new if old and "�" in old and new and "�" not in new else old

    conn.execute(
        "update message set snippet = %s, has_attachments = %s, parser_version = %s, subject = %s, from_name = %s,"
        " headers = headers || %s, is_automated = %s where id = %s",
        (p.snippet, any(a.is_attachment for a in p.attachments), mime.PARSER_VERSION,
         repaired(row["subject"], p.subject), repaired(row["from_name"], p.from_.name if p.from_ else None),
         Jsonb({**p.headers, "automated": p.automated_reasons, "defects": p.defects}), p.is_automated, mid))
    conn.execute(
        "insert into message_pattern (message_id, pattern_key, skeleton)"
        " select id, subject_pattern_key(from_address, subject), subject_skeleton(subject) from message where id = %s"
        " on conflict (message_id) do update set pattern_key = excluded.pattern_key, skeleton = excluded.skeleton",
        (mid,))
