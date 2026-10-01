"""A few thousand invented messages written straight into the tables, for query plans.

Going through the parser would take minutes; plans only need realistic row counts and
statistics. Nothing here is real mail. Most subjects and senders are filler, and a few
are rare on purpose (a Telia invoice, a Hemköp receipt, a nordvik.se sender), so that a
search is selective, as it is in the real archive.
"""

from __future__ import annotations

import psycopg

from talos import personal
from talos.ingest import SEARCH_SQL


def fill(conn: psycopg.Connection, n: int = 4000) -> None:
    conn.execute("insert into account (id, provider, address) values ('gmail', 'gmail', 'me@gmail.invalid'),"
                 " ('work', 'graph', 'me@work.invalid') on conflict do nothing")
    conn.execute(
        "with e as (insert into entity (kind) select 'thread' from generate_series(1, %s) returning id)"
        " insert into thread (id, account_id, subject) select id, 'gmail', 'Tråd ' || id from e", (n // 5,))
    conn.execute(
        """
        with e as (insert into entity (kind) select 'message' from generate_series(1, %(n)s) returning id),
             numbered as (select id, row_number() over (order by id) as i from e),
             threads as (select array_agg(id order by id) as a from thread)
        insert into message (id, account_id, provider_key, thread_id, direction, from_address, from_name, subject,
                             snippet, received_at, parser_version)
        select id, case when i %% 3 = 0 then 'work' else 'gmail' end, 'bulk-' || id,
               (select a[1 + (i %% array_length(a, 1))] from threads), 'in',
               case when i %% 101 = 0 then 'oskar@nordvik.se' else 'avsandare' || (i %% 700) || '@exempel' || (i %% 60) || '.se' end,
               'Avsändare ' || (i %% 700),
               case when i %% 97 = 0 then 'Din faktura från Telia ' when i %% 89 = 0 then 'Ditt kvitto från Hemköp '
                    else (array['Veckobrev nummer ', 'Möte om budget ', 'Leverans av order ', 'Påminnelse om tid ',
                                'Nyhetsbrev vecka ', 'Inbjudan till lunch ', 'Status för ärende '])[1 + (i %% 7)] end || i,
               'Utdrag ' || i, now() - make_interval(hours => i::int), 2
        from numbered
        """, {"n": n})
    conn.execute(
        f"""
        insert into message_text (message_id, body_kind, body_text, quote_stripped, search)
        select m.id, 'plain', b.body, b.body,
               {SEARCH_SQL.replace('%(subject)s', 'm.subject').replace('%(people)s', "m.from_name || ' ' || m.from_address")
                          .replace('%(filenames)s', "''").replace('%(body)s', 'b.body')}
        from message m
        cross join lateral (select 'Hej, här kommer veckans uppdatering om projektet och leveransen. Nummer '
                                   || m.id || '. Vänliga hälsningar' as body) b
        where m.provider_key like 'bulk-%'
        """)
    # A rule gives every message a topic; a human decides the topic of a few threads.
    conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref)"
                 " select id, 'topic', 't' || (id % 20), 'rule', 'rule:bulk@1' from message"
                 " where provider_key like 'bulk-%'")
    conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref)"
                 " select id, 'topic', 'human-thread', 'human', %s from thread where id %% 50 = 0", (personal.OWNER_ID,))
    # Flush the GIN pending lists, as autovacuum would; otherwise the planner prices the
    # fresh indexes as if they had to be read in full.
    for index in ("message_text_search_idx", "message_subject_trgm_idx", "message_from_trgm_idx"):
        conn.execute("select gin_clean_pending_list(%s::regclass)", (index,))
    conn.execute("analyze")
