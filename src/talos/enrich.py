"""Enrichment step A: the deterministic pre-pass, and its coverage report.

See docs/enrichment-plan.md §3–4 and §6 (stage A). No model and no network: plain SQL over
what ingest already stored. `prepass(conn)` does four things, each idempotent:

1. **Automated reasons.** The parser's reasons (`mime.ParsedMessage.automated_reasons`) are
   kept per message in `message.headers -> 'automated'`, a JSON list, which ingest has written
   since P0 and `talos reparse` now refreshes. A message without the key has them recomputed
   here from its stored headers (Auto-Submitted, Precedence, List-Id, List-Unsubscribe,
   Return-Path, the sender's local part): the same rules as the parser, without the vault.
2. **Subject patterns.** Every e-mail gets a pattern key, sender address + subject skeleton
   (`subject_skeleton` in SQL), in `message_pattern`; `subject_pattern` counts them.
3. **Sender profiles.** `sender_profile`: per (account, from address) the message count, how
   often the owner wrote to them, their threads the owner replied in, and the share of their mail with
   List-Unsubscribe, automated reasons or an event.
4. **Origin by rules**, only where the signals are unambiguous (below). Anything else stays
   empty for Jev (step C).

Origin signals. Each is a rule-made assignment with source_ref `prepass:origin.<signal>@<n>`,
n being the signal's own version (SIGNALS); a signal whose meaning changes gets a new n, so
the next run replaces its old values. `rules.run_all` deletes only `rule:…` refs, and the
pre-pass only its own, so neither wipes the other's. A message that already has an origin
from a rule in the rule table keeps that one (the pre-pass adds none).

The origins follow rules/taxonomy.json. People: person, person_via_system and list (people
writing to a list or group). Machine: every other value (MACHINE_ORIGINS).

Machine signals, first match wins:

| Signal         | Origin          | Fires when                                                              |
|----------------|-----------------|-------------------------------------------------------------------------|
| `quarantine`   | mail_system     | a quarantine or spam digest (QUARANTINE_SENDERS: Microsoft's            |
|                |                 | quarantine@messaging.microsoft.com and the like)                        |
| `event`        | system_report   | an event extractor made events from it, and all are backups with       |
|                |                 | status ok                                                                |
|                | alert           | … any other event: a failed, warning or info backup, an alarm, an alert |
| `type`         | alert           | its effective `type` is alert, security_event, alarm, incident, backup  |
|                |                 | or security                                                              |
|                | system_report   | … report                                                                |
|                | transactional   | … receipt, invoice, order, booking, shipping, subscription, statement,  |
|                |                 | payment, sign_in or password                                            |
|                | marketing       | … newsletter, promotion, event_webinar or survey                        |
|                | auto_reply      | … calendar_response                                                     |
|                | notification    | … notification                                                          |
| `teams_app`    | notification    | a Teams message posted by an application or bot                         |
| `bulk_mailer`  | marketing       | List-Unsubscribe, and Precedence bulk/junk or a marketing mailer's      |
|                |                 | X-Mailer; the owner never wrote to the sender; no receipt/order words  |
| `mailing_list` | list            | a List-Id with at least 3 senders, none of them over half of its mail;  |
|                |                 | not a no-reply sender, not bulk, not a marketing mailer                 |
| `bounce`       | mail_system     | from mailer-daemon/postmaster, or a delivery-failure subject            |
|                |                 | (BOUNCE_SUBJECT) on an auto-reply or with a null Return-Path            |
| `auto_reply`   | auto_reply      | any other auto-reply (Auto-Submitted: auto-replied) or null Return-Path |
|                |                 | (out of office, mostly)                                                 |

The bounce signal of version 1 gave notification to all of the last two rows; most of them
turned out to be out-of-office replies, which the taxonomy calls auto_reply.

Person signals, first match wins (e-mail: never with List-Id, List-Unsubscribe or automated reasons):

| Signal          | Origin            | Fires when                                                           |
|-----------------|-------------------|----------------------------------------------------------------------|
| `teams_user`    | person            | a Teams message written by a user                                    |
| `via_name`      | person_via_system | the sender's name is a person's name + "via …" ("Anna Berg via X");  |
|                 |                   | List-Id and automated reasons allowed, List-Unsubscribe not          |
| `sender_header` | person_via_system | the Sender header names another address than From, and the owner    |
|                 |                   | has written to the From address                                      |
| `thread_reply`  | person            | the owner has written to the sender, in a thread they wrote in       |
| `correspondent` | person            | the owner wrote to the sender at least twice and wrote in one of     |
|                 |                   | their threads; at most 1 in 10 of their messages carries a machine   |
|                 |                   | signal; and the message's subject template spans fewer than 3 threads |

When a machine signal and a person signal both fire, the message stays undecided: that
disagreement is exactly what Jev is for (mailing_list counts as a machine signal here, as
before, though list is a people origin). Change a signal → bump its version in SIGNALS.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import psycopg

from talos import mime

VERSION = 2  # the pre-pass as a whole; each signal's own version is in SIGNALS
PREFIX = "prepass:origin."
# The origins of rules/taxonomy.json, in its order: people first, then machines.
ORIGINS = ("person", "person_via_system", "list", "auto_reply", "transactional", "notification", "alert",
           "system_report", "mail_system", "marketing", "spam")
PERSON_ORIGINS = ("person", "person_via_system", "list")
MACHINE_ORIGINS = tuple(o for o in ORIGINS if o not in PERSON_ORIGINS)
# Each signal's version, in its source_ref. v2: the taxonomy's origins (2026-09-25).
SIGNALS = {"quarantine": 1, "event": 2, "type": 2, "teams_app": 1, "bulk_mailer": 1, "mailing_list": 1,
           "bounce": 2, "auto_reply": 1, "teams_user": 1, "via_name": 1, "sender_header": 1, "thread_reply": 1,
           "correspondent": 1}
TYPE_ORIGINS = {
    "alert": ("alert", "security_event", "alarm", "incident", "backup", "security"),
    "system_report": ("report",),
    "transactional": ("receipt", "invoice", "order", "booking", "shipping", "subscription", "statement", "payment",
                      "sign_in", "password"),
    "marketing": ("newsletter", "promotion", "event_webinar", "survey"),
    "auto_reply": ("calendar_response",),
    "notification": ("notification",),
}
# Quarantine and spam digests: the mail system's own reports. Narrow on purpose (the sender).
QUARANTINE_SENDERS = r"^(quarantine@messaging\.microsoft\.com|quarantine@[a-z0-9.-]+\.protection\.outlook\.com)$"
# A delivery-failure notice (DSN/NDR), by its subject: English, Swedish and Exchange's forms.
BOUNCE_SUBJECT = (r"^\s*(undeliverable|olevererbart|delivery status notification|mail delivery (failed|failure|subsystem)"
                  r"|returned mail|undelivered mail|delivery (has )?failed|delayed message|fördröjd leverans"
                  r"|message delivery failure|non-?delivery|failure notice|kan inte levereras|levererades inte)")

# X-Mailer of marketing platforms (not of transactional relays such as Mailgun or SES).
MARKETING_MAILERS = (r"(mailchimp|getresponse|nlserver|sailthru|activecampaign|sendinblue|brevo|create ?send|"
                     r"campaign ?monitor|carma|apsis|klaviyo|mailerlite|rule\.io|ungapped|hubspot|marketo|"
                     r"exacttarget|emarsys|dotdigital|mailjet|substack|beehiiv|convertkit)")
# Words that make a bulk mail possibly a receipt or an order: then it is not called marketing.
TRANSACTIONAL_WORDS = (r"\m(kvitto|receipt|faktura|invoice|order|beställning|betalning|payment|bokning|booking|"
                       r"biljett|ticket|leverans|delivery|paket|package|bekräftelse|confirmation|köp|purchase)")
# A person's name (two or more capitalised words) followed by "via …": "Anna Berg via Blocket",
# "Anna Berg (via Google Docs)". "NASA via X" or "Simployer via Y" do not qualify.
VIA_PERSON = r"""^\s*"?[[:upper:]][[:lower:]]+(\s+[[:upper:]][[:lower:]'’.-]*)+\s+\(?[vV]ia\s+\S"""

LIST_MIN_SENDERS = 3
LIST_MAX_SENDER_SHARE = 0.5
CORRESPONDENT_MIN_WRITTEN = 2
CORRESPONDENT_MAX_MACHINE_SHARE = 0.1
TEMPLATE_THREADS = 3  # a subject template seen in this many threads is a machine's, not a letter

NOISE_TYPES = ("notification", "backup", "alert", "newsletter")  # the plan's "machine mail" (§0)
TEAMS_WINDOW_GAP = "2 hours"
TEAMS_WINDOW_CAP = 30


# ---------------------------------------------------------------- 1. automated reasons

def reasons_sql(m: str = "m") -> str:
    """SQL for the parser's automated reasons (mime._parse), from what a message row stores."""
    h = f"{m}.headers"
    return (
        "to_jsonb(array_remove(array["
        f"case when lower(coalesce({h} ->> 'auto_submitted', '')) not in ('', 'no') then 'auto-submitted' end, "
        f"case when lower(coalesce({h} ->> 'precedence', '')) in ('bulk', 'list', 'junk') then 'precedence' end, "
        f"case when {m}.list_id is not null or coalesce({h} ->> 'list_unsubscribe', '') <> '' then 'list' end, "
        f"case when split_part({m}.from_address, '@', 1) ~* %(noreply)s then 'noreply-sender' end, "
        f"case when trim(coalesce({h} ->> 'return_path', '')) = '<>' then 'null-return-path' end"
        "], null))")


def backfill_reasons(conn: psycopg.Connection, *, batch: int = 5000) -> int:
    """Store the automated reasons of every e-mail that has none stored. Batches commit on
    their own when the connection is in autocommit, so an interrupted run resumes; a message
    done is never read again. Returns how many were filled."""
    total, last_id = 0, 0
    while True:
        with conn.transaction():
            row = conn.execute(
                "select max(id) as hi, count(*) as n from (select id from message where medium = 'email'"
                " and not headers ? 'automated' and id > %s order by id limit %s) x", (last_id, batch)).fetchone()
            if not row["n"]:
                return total
            cur = conn.execute(
                f"update message m set headers = m.headers || jsonb_build_object('automated', {reasons_sql()})"
                " where m.medium = 'email' and not m.headers ? 'automated' and m.id > %(lo)s and m.id <= %(hi)s",
                {"noreply": mime._NOREPLY.pattern, "lo": last_id, "hi": row["hi"]})
            total += cur.rowcount
            last_id = row["hi"]


# ---------------------------------------------------------------- 2. subject patterns

def patterns(conn: psycopg.Connection, *, full: bool = False) -> dict:
    """Give every e-mail without one its pattern key, then rebuild subject_pattern.

    Ingest writes the key of a new mail and reparse refreshes a repaired subject, so only
    messages without a key are read. full=True recomputes every key and rewrites those that
    differ (after a change to subject_skeleton). Idempotent: a second run writes nothing."""
    with conn.transaction():
        if full:
            changed = conn.execute(
                "insert into message_pattern as p (message_id, pattern_key, skeleton)"
                " select m.id, subject_pattern_key(m.from_address, m.subject), subject_skeleton(m.subject)"
                " from message m where m.medium = 'email'"
                " on conflict (message_id) do update set pattern_key = excluded.pattern_key,"
                " skeleton = excluded.skeleton where p.pattern_key is distinct from excluded.pattern_key").rowcount
        else:
            changed = conn.execute(
                "insert into message_pattern (message_id, pattern_key, skeleton)"
                " select m.id, subject_pattern_key(m.from_address, m.subject), subject_skeleton(m.subject)"
                " from message m where m.medium = 'email'"
                " and not exists (select 1 from message_pattern p where p.message_id = m.id)").rowcount
        # Only rows whose numbers changed are written, so a rerun leaves the table as it was.
        written = conn.execute(
            """
            insert into subject_pattern as s (pattern_key, from_address, skeleton, message_count, thread_count,
                                              accounts, first_at, last_at, sample_ids)
            select pattern_key, from_address, skeleton, n, threads, accounts, first_at, last_at,
                   case when n = 1 then array[ids[1]] when n = 2 then ids[1:2]
                        else array[ids[1], ids[(n + 1) / 2], ids[n]] end
            from (
                select p.pattern_key, min(lower(coalesce(m.from_address, ''))) as from_address, min(p.skeleton) as skeleton,
                       count(*)::int as n, count(distinct m.thread_id)::int as threads,
                       array_agg(distinct m.account_id) as accounts, min(m.received_at) as first_at,
                       max(m.received_at) as last_at,
                       array_agg(m.id order by m.received_at nulls first, m.id) as ids
                from message_pattern p join message m on m.id = p.message_id
                group by p.pattern_key
            ) x
            on conflict (pattern_key) do update set
                message_count = excluded.message_count, thread_count = excluded.thread_count,
                accounts = excluded.accounts, first_at = excluded.first_at, last_at = excluded.last_at,
                sample_ids = excluded.sample_ids, computed_at = now()
            where (s.message_count, s.thread_count, s.accounts, s.first_at, s.last_at, s.sample_ids)
                  is distinct from (excluded.message_count, excluded.thread_count, excluded.accounts,
                                    excluded.first_at, excluded.last_at, excluded.sample_ids)
            """).rowcount
        gone = conn.execute("delete from subject_pattern s where not exists"
                            " (select 1 from message_pattern p where p.pattern_key = s.pattern_key)").rowcount
        n = conn.execute("select count(*)::int as n from subject_pattern").fetchone()["n"]
    return {"keys_written": changed, "patterns": n, "patterns_written": written, "patterns_removed": gone}


# ---------------------------------------------------------------- 3. sender profiles

def profiles(conn: psycopg.Connection) -> dict:
    """Rebuild sender_profile from incoming mail and the owner's outgoing messages. Only rows whose
    numbers changed are written."""
    with conn.transaction():
        written = conn.execute(
            """
            with outp as (
                select o.account_id, p.address, count(distinct o.id)::int as n, max(o.received_at) as last_at
                from message o join participant p on p.message_id = o.id and p.role in ('to', 'cc', 'bcc')
                where o.direction = 'out' group by 1, 2),
            outall as (select address, sum(n)::int as n, max(last_at) as last_at from outp group by 1),
            tout as (select distinct thread_id from message where direction = 'out' and thread_id is not null),
            ev as (select distinct message_id from event)
            insert into sender_profile as sp (account_id, address, message_count, first_at, last_at, written_to,
                written_to_all, last_written_at, threads, replied_threads, list_unsubscribe_share, automated_share,
                event_share, templates)
            select m.account_id, m.from_address, count(*)::int, min(m.received_at), max(m.received_at),
                   coalesce(max(op.n), 0), coalesce(max(oa.n), 0), max(oa.last_at),
                   count(distinct m.thread_id)::int,
                   count(distinct m.thread_id) filter (where t.thread_id is not null)::int,
                   avg((coalesce(m.headers ->> 'list_unsubscribe', '') <> '')::int)::real,
                   avg((m.is_automated or jsonb_array_length(coalesce(m.headers -> 'automated', '[]')) > 0)::int)::real,
                   avg((ev.message_id is not null)::int)::real,
                   count(distinct mp.skeleton)::int
            from message m
            left join tout t on t.thread_id = m.thread_id
            left join ev on ev.message_id = m.id
            left join message_pattern mp on mp.message_id = m.id
            left join outp op on op.account_id = m.account_id and op.address = m.from_address
            left join outall oa on oa.address = m.from_address
            where m.direction = 'in' and m.from_address is not null
            group by m.account_id, m.from_address
            on conflict (account_id, address) do update set
                message_count = excluded.message_count, first_at = excluded.first_at, last_at = excluded.last_at,
                written_to = excluded.written_to, written_to_all = excluded.written_to_all,
                last_written_at = excluded.last_written_at, threads = excluded.threads,
                replied_threads = excluded.replied_threads, list_unsubscribe_share = excluded.list_unsubscribe_share,
                automated_share = excluded.automated_share, event_share = excluded.event_share,
                templates = excluded.templates, computed_at = now()
            where (sp.message_count, sp.first_at, sp.last_at, sp.written_to, sp.written_to_all, sp.last_written_at,
                   sp.threads, sp.replied_threads, sp.list_unsubscribe_share, sp.automated_share, sp.event_share,
                   sp.templates)
                  is distinct from (excluded.message_count, excluded.first_at, excluded.last_at, excluded.written_to,
                   excluded.written_to_all, excluded.last_written_at, excluded.threads, excluded.replied_threads,
                   excluded.list_unsubscribe_share, excluded.automated_share, excluded.event_share,
                   excluded.templates)
            """).rowcount
        gone = conn.execute(
            "delete from sender_profile sp where not exists (select 1 from message m where m.from_address = sp.address"
            " and m.account_id = sp.account_id and m.direction = 'in')").rowcount
        n = conn.execute("select count(*)::int as n from sender_profile").fetchone()["n"]
    return {"profiles": n, "written": written, "removed": gone}


# ---------------------------------------------------------------- 4. origin

def _pick(value: str, signal: str) -> str:
    """The SQL literal a signal's CASE branch gives: 'value|signal@version'."""
    return f"'{value}|{signal}@{SIGNALS[signal]}'"


def _machine_sql() -> str:
    types = "\n".join(f"        when f.type in ({', '.join(repr(t) for t in ts)}) then {_pick(o, 'type')}"
                      for o, ts in TYPE_ORIGINS.items())
    auto = ("(f.auto_submitted like 'auto-replied%%' or f.reasons ? 'null-return-path'"
            " or split_part(f.from_address, '@', 1) ~* '^(mailer-daemon|postmaster)$')")
    return f"""case
        when f.medium = 'email' and lower(f.from_address) ~ %(quarantine)s then {_pick('mail_system', 'quarantine')}
        when f.event and f.event_report_only then {_pick('system_report', 'event')}
        when f.event then {_pick('alert', 'event')}
{types}
        when f.medium <> 'email' and f.is_automated then {_pick('notification', 'teams_app')}
        when f.medium = 'email' and f.lu and (f.prec in ('bulk', 'junk') or f.x_mailer ~* %(mailers)s)
             and f.written_to = 0 and f.subject !~* %(tx)s then {_pick('marketing', 'bulk_mailer')}
        when f.medium = 'email' and f.list_id is not null and f.list_senders >= {LIST_MIN_SENDERS}
             and f.list_top_share <= {LIST_MAX_SENDER_SHARE} and not f.reasons ? 'noreply-sender'
             and f.prec not in ('bulk', 'junk') and f.x_mailer !~* %(mailers)s then {_pick('list', 'mailing_list')}
        when f.medium = 'email' and {auto} and (split_part(f.from_address, '@', 1) ~* '^(mailer-daemon|postmaster)$'
             or f.subject ~* %(bounce)s) then {_pick('mail_system', 'bounce')}
        when f.medium = 'email' and {auto} then {_pick('auto_reply', 'auto_reply')}
    end"""


def _person_sql() -> str:
    clean = "f.medium = 'email' and not f.automated and not f.lu and f.list_id is null"
    return f"""case
        when f.medium <> 'email' and not f.is_automated then {_pick('person', 'teams_user')}
        when f.medium = 'email' and not f.lu and f.from_name ~ %(via)s then {_pick('person_via_system', 'via_name')}
        when {clean} and f.sender_differs and f.written_to > 0 then {_pick('person_via_system', 'sender_header')}
        when {clean} and f.written_to > 0 and f.replied then {_pick('person', 'thread_reply')}
        when {clean} and f.written_to >= {CORRESPONDENT_MIN_WRITTEN} and f.replied_threads >= 1
             and greatest(f.automated_share, f.list_unsubscribe_share, f.event_share) <= {CORRESPONDENT_MAX_MACHINE_SHARE}
             and f.template_threads < {TEMPLATE_THREADS} then {_pick('person', 'correspondent')}
    end"""


def _features(conn: psycopg.Connection) -> None:
    """pp_f: one row per incoming message with every input the origin signals read."""
    conn.execute("drop table if exists pp_type, pp_list, pp_f, pp_want")
    # The effective type, message and thread together, ranked as effective_message_assignment does.
    conn.execute(
        """
        create temp table pp_type on commit drop as
        select distinct on (x.message_id) x.message_id, x.value from (
            select m.id as message_id, a.value, a.source_kind, a.created_at, a.id, true as own
            from assignment a join message m on m.id = a.entity_id
            where a.dimension_id = 'type' and a.status = 'active'
            union all
            select m.id, a.value, a.source_kind, a.created_at, a.id, false
            from assignment a join message m on m.thread_id = a.entity_id
            where a.dimension_id = 'type' and a.status = 'active'
        ) x
        order by x.message_id,
                 case x.source_kind when 'human' then 4 when 'rule' then 3 when 'model' then 2 else 1 end desc,
                 x.own desc, x.created_at desc, x.id desc
        """)
    conn.execute("create index on pp_type (message_id)")
    conn.execute(
        """
        create temp table pp_list on commit drop as
        select list_id, count(*)::int as senders, max(n)::real / sum(n) as top_share
        from (select list_id, from_address, count(*) as n from message
              where direction = 'in' and list_id is not null group by 1, 2) s
        group by list_id
        """)
    conn.execute(
        """
        create temp table pp_f on commit drop as
        with tout as (select distinct thread_id from message where direction = 'out' and thread_id is not null),
             ev as (select message_id, bool_and(kind = 'backup' and status = 'ok') as report_only
                    from event group by 1),
             sd as (select distinct p.message_id from participant p join message m on m.id = p.message_id
                    where p.role = 'sender' and p.address <> m.from_address)
        select m.id, m.account_id, m.medium, m.from_address, coalesce(m.from_name, '') as from_name,
               coalesce(m.subject, '') as subject, m.list_id, m.is_automated,
               coalesce(m.headers -> 'automated', '[]') as reasons,
               jsonb_array_length(coalesce(m.headers -> 'automated', '[]')) > 0 as automated,
               coalesce(m.headers ->> 'list_unsubscribe', '') <> '' as lu,
               lower(coalesce(m.headers ->> 'precedence', '')) as prec,
               coalesce(m.headers ->> 'x_mailer', '') as x_mailer,
               lower(coalesce(m.headers ->> 'auto_submitted', '')) as auto_submitted,
               ev.message_id is not null as event, coalesce(ev.report_only, false) as event_report_only,
               ty.value as type,
               sd.message_id is not null as sender_differs,
               tout.thread_id is not null as replied,
               coalesce(sp.written_to_all, 0) as written_to, coalesce(sp.replied_threads, 0) as replied_threads,
               coalesce(sp.automated_share, 1) as automated_share,
               coalesce(sp.list_unsubscribe_share, 1) as list_unsubscribe_share,
               coalesce(sp.event_share, 1) as event_share,
               coalesce(pt.thread_count, 0) as template_threads,
               coalesce(ls.senders, 0) as list_senders, coalesce(ls.top_share, 1) as list_top_share
        from message m
        left join ev on ev.message_id = m.id
        left join pp_type ty on ty.message_id = m.id
        left join sd on sd.message_id = m.id
        left join tout on tout.thread_id = m.thread_id
        left join sender_profile sp on sp.account_id = m.account_id and sp.address = m.from_address
        left join message_pattern mp on mp.message_id = m.id
        left join subject_pattern pt on pt.pattern_key = mp.pattern_key
        left join pp_list ls on ls.list_id = m.list_id
        where m.direction = 'in'
        """)


def origin(conn: psycopg.Connection) -> dict:
    """Assign origin where the signals are unambiguous; remove the pre-pass's own values that
    no longer hold. A re-run with nothing new writes nothing. Returns counts."""
    params = {"mailers": MARKETING_MAILERS, "tx": TRANSACTIONAL_WORDS, "via": VIA_PERSON,
              "quarantine": QUARANTINE_SENDERS, "bounce": BOUNCE_SUBJECT}
    with conn.transaction():
        _features(conn)
        conn.execute(
            f"""
            create temp table pp_want on commit drop as
            select id, machine, person,
                   case when machine is not null and person is not null then null
                        else coalesce(machine, person) end as pick
            from (select f.id, {_machine_sql()} as machine, {_person_sql()} as person from pp_f f) s
            """, params)
        # A rule from the rule table that sets origin is the owner's explicit word: leave those messages be.
        conn.execute(
            "delete from pp_want w where exists (select 1 from assignment a where a.entity_id = w.id"
            " and a.dimension_id = 'origin' and a.status = 'active' and a.source_kind = 'rule'"
            " and a.source_ref like %s)", ("rule:%",))
        conn.execute(
            "alter table pp_want add column value text, add column ref text;"
            f" update pp_want set value = split_part(pick, '|', 1),"
            f" ref = '{PREFIX}' || split_part(pick, '|', 2) where pick is not null;"
            " create index on pp_want (id);")
        removed = conn.execute(
            "delete from assignment a where a.source_kind = 'rule' and a.source_ref like %s"
            " and not exists (select 1 from pp_want w where w.id = a.entity_id and w.value = a.value"
            " and w.ref = a.source_ref and a.dimension_id = 'origin' and a.status = 'active')",
            (PREFIX + "%",)).rowcount
        added = conn.execute(
            "insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, confidence, evidence)"
            " select w.id, 'origin', w.value, 'rule', w.ref, 1.0,"
            " jsonb_build_object('signal', split_part(split_part(w.pick, '|', 2), '@', 1),"
            " 'version', split_part(w.pick, '@', 2)::int)"
            " from pp_want w where w.pick is not null and not exists (select 1 from assignment a"
            " where a.entity_id = w.id and a.dimension_id = 'origin' and a.source_kind = 'rule'"
            " and a.source_ref = w.ref and a.value = w.value and a.status = 'active')").rowcount
        signals = {r["pick"]: r["n"] for r in conn.execute(
            "select coalesce(pick, case when machine is not null then 'conflict' else 'undecided' end) as pick,"
            " count(*)::int as n from pp_want group by 1 order by 2 desc")}
    return {"added": added, "removed": removed, "by_signal": signals}


# ---------------------------------------------------------------- the whole pre-pass


def prepass(conn: psycopg.Connection, *, dry_run: bool = False, full: bool = False) -> dict:
    """Steps 1–4 in order. With dry_run everything runs in one transaction that is rolled
    back, so the counts are exact and nothing stays. full=True recomputes every pattern key.
    The connection should be in autocommit (the CLI's is), so a real run commits each step."""
    timings: dict[str, float] = {}
    out: dict = {"version": VERSION, "dry_run": dry_run}

    def step(name, fn):
        t = time.monotonic()
        out[name] = fn(conn)
        timings[name] = round(time.monotonic() - t, 2)

    steps = [("reasons", backfill_reasons), ("patterns", lambda c: patterns(c, full=full)),
             ("profiles", profiles), ("origin", origin)]
    started = time.monotonic()
    if dry_run:
        with conn.transaction():  # everything, then rolled back: exact counts, nothing kept
            for name, fn in steps:
                step(name, fn)
            raise psycopg.Rollback()
    else:
        for name, fn in steps:
            step(name, fn)
    out["seconds"] = {**timings, "total": round(time.monotonic() - started, 2)}
    return out


# ---------------------------------------------------------------- the coverage report

def report(conn: psycopg.Connection) -> dict:
    """Coverage of origin, pattern statistics and the estimated number of Jev cases (§4.2)."""
    started = time.monotonic()
    with conn.transaction():
        conn.execute("drop table if exists rp_in")
        # Every incoming message with its effective origin (message and thread, ranked as
        # effective_message_assignment ranks them) and whether it is machine mail by the
        # plan's §0 definition: automated, an event, or a rule type of a noise kind.
        conn.execute(
            """
            create temp table rp_in on commit drop as
            with o as (
                select distinct on (x.message_id) x.message_id, x.value, x.source_ref from (
                    select m.id as message_id, a.value, a.source_kind, a.source_ref, a.created_at, a.id, true as own
                    from assignment a join message m on m.id = a.entity_id
                    where a.dimension_id = 'origin' and a.status = 'active'
                    union all
                    select m.id, a.value, a.source_kind, a.source_ref, a.created_at, a.id, false
                    from assignment a join message m on m.thread_id = a.entity_id
                    where a.dimension_id = 'origin' and a.status = 'active'
                ) x
                order by x.message_id,
                         case x.source_kind when 'human' then 4 when 'rule' then 3 when 'model' then 2 else 1 end desc,
                         x.own desc, x.created_at desc, x.id desc),
            noise as (select distinct entity_id as message_id from assignment
                      where dimension_id = 'type' and status = 'active' and value = any(%(noise)s)),
            ev as (select distinct message_id from event)
            select m.id, m.account_id, m.medium, m.thread_id, m.from_address, m.received_at,
                   o.value as origin, o.source_ref,
                   (m.medium = 'email' and (m.is_automated or ev.message_id is not null or noise.message_id is not null))
                       as machine,
                   mp.pattern_key
            from message m
            left join o on o.message_id = m.id
            left join ev on ev.message_id = m.id
            left join noise on noise.message_id = m.id
            left join message_pattern mp on mp.message_id = m.id
            where m.direction = 'in'
            """, {"noise": list(NOISE_TYPES)})
        conn.execute("analyze rp_in")

        coverage = conn.execute(
            "select account_id, medium, sum(n)::int as incoming, coalesce(sum(n) filter (where origin is not null), 0)::int"
            " as decided, jsonb_object_agg(origin, n) filter (where origin is not null) as by_value"
            " from (select account_id, medium, origin, count(*)::int as n from rp_in group by 1, 2, 3) x"
            " group by 1, 2 order by 1, 2").fetchall()
        for c in coverage:
            c["undecided"] = c["incoming"] - c["decided"]
            c["decided_share"] = round(c["decided"] / c["incoming"], 4) if c["incoming"] else 0
        email = conn.execute(
            "select count(*)::int as incoming, count(origin)::int as decided,"
            " count(*) filter (where origin is null)::int as undecided,"
            " count(*) filter (where origin is null and machine)::int as undecided_machine,"
            " count(*) filter (where origin is null and not machine)::int as undecided_other"
            " from rp_in where medium = 'email'").fetchone()
        email["undecided_share"] = round(email["undecided"] / email["incoming"], 4) if email["incoming"] else 0
        by_value = {r["origin"]: r["n"] for r in conn.execute(
            "select origin, count(*)::int as n from rp_in where medium = 'email' and origin is not null"
            " group by 1 order by 2 desc")}
        by_signal = {r["source_ref"]: r["n"] for r in conn.execute(
            "select source_ref, count(*)::int as n from rp_in where origin is not null group by 1 order by 2 desc")}

        pat = conn.execute(
            """
            with mm as (select pattern_key, count(*) as n from rp_in where machine and pattern_key is not null group by 1)
            select (select count(*) from rp_in where machine)::int as machine_mail,
                   (select count(distinct from_address) from rp_in where machine)::int as machine_senders,
                   count(*)::int as machine_templates,
                   count(*) filter (where n >= 3)::int as templates_3plus,
                   coalesce(sum(n) filter (where n >= 3), 0)::int as covered_by_3plus,
                   (select count(*) from subject_pattern)::int as all_patterns
            from mm
            """).fetchone()
        pat["share_covered_by_3plus"] = round(pat["covered_by_3plus"] / pat["machine_mail"], 4) \
            if pat["machine_mail"] else 0

        jev = {
            "machine_all": _machine_cases(conn, "machine"),
            "machine_undecided": _machine_cases(conn, "machine and origin is null"),
            "person_threads": _person_cases(conn),
            "teams_windows": _teams_windows(conn),
        }
        jev["total_plan_method"] = (jev["machine_all"]["cases"] + jev["person_threads"]["cases"]
                                    + jev["teams_windows"]["capped"])
    return {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "version": VERSION,
            "coverage": coverage, "email": {**email, "by_value": by_value}, "by_signal": by_signal,
            "patterns": pat, "jev_estimate": jev, "seconds": round(time.monotonic() - started, 2)}


def _machine_cases(conn: psycopg.Connection, where: str) -> dict:
    """Plan §4.2: 3 samples per template with at least 3 messages, plus one sample per
    sender for the long tail (their templates with fewer than 3)."""
    return conn.execute(
        f"""
        with t as (select pattern_key, from_address, count(*) as n from rp_in where {where} group by 1, 2)
        select coalesce(sum(n), 0)::int as messages,
               count(*) filter (where n >= 3)::int as templates_3plus,
               (3 * count(*) filter (where n >= 3))::int as template_cases,
               count(distinct from_address) filter (where n < 3)::int as tail_senders,
               (3 * count(*) filter (where n >= 3) + count(distinct from_address) filter (where n < 3))::int as cases
        from t
        """).fetchone()


def _person_cases(conn: psycopg.Connection) -> dict:
    """Person-side e-mail (not machine mail): one case per thread, except the bulk unknown
    senders (20+ messages, never written to, no thread the owner replied in), which count as
    patterns like machine mail."""
    return conn.execute(
        """
        with p as (select r.*, coalesce(sp.written_to_all, 0) = 0 and coalesce(sp.replied_threads, 0) = 0
                              and coalesce(sp.message_count, 0) >= 20 as bulk
                   from rp_in r left join sender_profile sp on sp.account_id = r.account_id
                                                          and sp.address = r.from_address
                   where r.medium = 'email' and not r.machine),
             t as (select pattern_key, from_address, count(*) as n from p where bulk group by 1, 2)
        select (select count(*) from p)::int as messages,
               (select count(distinct coalesce(thread_id, -id)) from p where not bulk)::int as threads,
               (select count(*) from p where bulk)::int as bulk_messages,
               (select count(distinct from_address) from p where bulk)::int as bulk_senders,
               (select 3 * count(*) filter (where n >= 3) + count(distinct from_address) filter (where n < 3)
                from t)::int as bulk_cases,
               (select count(distinct coalesce(thread_id, -id)) from p where not bulk)::int
               + (select 3 * count(*) filter (where n >= 3) + count(distinct from_address) filter (where n < 3)
                  from t)::int as cases
        """).fetchone()


def _teams_windows(conn: psycopg.Connection) -> dict:
    """Plan §5: runs of messages in one chat with no gap over 2 hours; capped windows split
    every TEAMS_WINDOW_CAP messages."""
    return conn.execute(
        f"""
        with m as (select thread_id, received_at,
                          case when received_at - lag(received_at) over (partition by thread_id order by received_at, id)
                                    <= interval '{TEAMS_WINDOW_GAP}' then 0 else 1 end as starts
                   from message where medium <> 'email' and thread_id is not null),
             w as (select thread_id, sum(starts) over (partition by thread_id order by received_at
                                                        rows unbounded preceding) as w from m),
             n as (select thread_id, w, count(*) as n from w group by 1, 2)
        select (select count(*) from message where medium <> 'email')::int as messages,
               count(*)::int as windows, coalesce(sum(ceil(n::numeric / {TEAMS_WINDOW_CAP})), 0)::int as capped,
               coalesce(round(avg(n), 1), 0)::float as mean_messages,
               coalesce(percentile_cont(0.5) within group (order by n), 0)::float as median_messages
        from n
        """).fetchone()


def write_report(rep: dict, logs: Path) -> Path:
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / f"enrich-report-{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
    path.write_text(json.dumps(rep, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def format_report(rep: dict) -> str:
    lines = ["Origin coverage (incoming):",
             f"  {'account':<8} {'medium':<11} {'incoming':>9} {'decided':>9} {'share':>7}  values"]
    for c in rep["coverage"]:
        values = ", ".join(f"{k} {v}" for k, v in sorted((c["by_value"] or {}).items(), key=lambda kv: -kv[1]))
        lines.append(f"  {c['account_id']:<8} {c['medium']:<11} {c['incoming']:>9} {c['decided']:>9}"
                     f" {c['decided_share']:>7.1%}  {values}")
    e = rep["email"]
    lines += ["", f"Incoming e-mail: {e['incoming']}, decided {e['decided']}, undecided {e['undecided']}"
                  f" ({e['undecided_share']:.1%}; {e['undecided_machine']} of them machine mail)",
              "  by value: " + ", ".join(f"{k} {v}" for k, v in e["by_value"].items()),
              "  by signal: " + ", ".join(f"{k.removeprefix(PREFIX)} {v}" for k, v in rep["by_signal"].items() if k)]
    p = rep["patterns"]
    lines += ["", f"Machine mail: {p['machine_mail']} messages from {p['machine_senders']} senders,"
                  f" {p['machine_templates']} templates; {p['templates_3plus']} templates with 3+ messages cover"
                  f" {p['covered_by_3plus']} ({p['share_covered_by_3plus']:.1%}). All patterns: {p['all_patterns']}."]
    j = rep["jev_estimate"]
    ma, mu, pt, tw = j["machine_all"], j["machine_undecided"], j["person_threads"], j["teams_windows"]
    lines += ["", "Estimated Jev cases (plan §4.2):",
              f"  machine patterns: {ma['cases']} ({ma['templates_3plus']} templates × 3 + {ma['tail_senders']}"
              f" tail senders) for {ma['messages']} messages; of which undecided origin only: {mu['cases']}",
              f"  person threads:   {pt['cases']} ({pt['threads']} threads + {pt['bulk_cases']} for"
              f" {pt['bulk_messages']} messages from {pt['bulk_senders']} bulk unknown senders)",
              f"  Teams windows:    {tw['capped']} ({tw['windows']} windows of 2 h, split at {TEAMS_WINDOW_CAP};"
              f" mean {tw['mean_messages']}, median {tw['median_messages']})",
              f"  total:            {j['total_plan_method']}"]
    return "\n".join(lines)
