"""Importance: which messages deserve the owner's attention, decided deterministically.

Volume is not what matters; fifty newsletters a day are noise. Every message gets a
score, the sum of the signals below that fire, and every signal that fired is kept in
`message_importance.reasons` with its points, so any score can be explained, as every
value in Talos says where it came from. A model may add proposals later; it is not
part of this.

Signals (version 1). They apply to incoming messages; the owner's own (`out`, `self`)
score 0 and are `low`.

| Signal          | Points | Fires when                                                                 |
|-----------------|-------:|----------------------------------------------------------------------------|
| `person`        |   +20  | not `is_automated`, a sender address, and not a no-reply-style address      |
| `direct`        |   +20  | mail: one of `my_address` in To, no List-Id/List-Unsubscribe, at most 10    |
|                 |        | To+Cc recipients; Teams chat: the owner is a member and the chat has at    |
|                 |        | most 5 other members (`to` participants)                                   |
| `copied`        |    +5  | not direct, but the owner is on Cc/Bcc, in To of a broadcast (>10          |
|                 |        | recipients), or in a bigger Teams chat; never for list mail                |
| `replied_often` |   +25  | the owner sent at least 5 messages to the sender in the last 2 years       |
| `replied`       |   +15  | the owner sent 1 to 4 messages to the sender in the last 2 years           |
| `in_thread`     |   +15  | the owner has an outgoing message in the same thread (not counted for      |
|                 |        | Teams chats, where the thread is the chat itself)                          |
| `question`      |   +20  | a person asks the owner directly (`?` or phrasing such as "kan du",        |
|                 |        | "could you") in the subject or the quote-stripped text, and the owner has  |
|                 |        | not written in the thread since. This is what makes a message `waiting`.   |
| `answered`      |     0  | the same question, but the owner has written in the thread since (explains |
|                 |        | only)                                                                      |
| `deadline`      |   +10  | a person mentions a deadline ("senast", "deadline", "by Friday", "före" …) |
|                 |        | or a date within 14 days after the message (2026-09-30, 30/9, 30 sep)       |
| `vip`           |   +50  | the sender's address or domain is in `importance_vip`                      |
| `filed_noise`   |   -60  | the effective `type` is newsletter, notification, alert or backup, or the  |
|                 |        | effective `topic` is Newsletters (`effective_message_assignment`)           |
| `list`          |   -40  | a List-Id or List-Unsubscribe header                                       |
| `automated`     |   -30  | `is_automated` (auto-submitted, bulk precedence, list, no-reply sender …)  |
| `marked`        |     0  | the owner's own mark in the `importance` dimension; overrides the level    |
|                 |        | (below)                                                                    |

Levels, from the score:

| Level    | Score        | Also                                                                        |
|----------|--------------|-----------------------------------------------------------------------------|
| `high`   | 70 or more   | or the owner marked it `important` (whatever the score)                     |
| `medium` | 45 to 69     |                                                                             |
| `low`    | 10 to 44     | the owner's own messages; and at most `low` whenever a noise signal fired   |
| `noise`  | below 10     | or the owner marked it `not_important` (whatever the score)                 |

So noise (`filed_noise`, `list`, `automated`) never ranks as important, and a human mark
beats every signal. Some examples: a colleague the owner often answers who asks them something
directly scores 20+20+25+20 = 85 (high); the same message after the owner's reply 20+20+25+15 = 80
(high, no longer waiting); an unknown person writing to the owner directly 40 (low); a
newsletter sent "to" the owner 0-40-30 = -70 (noise).

Recompute: `compute(conn)` scores every message in bulk SQL, a handful of statements over
temporary tables, never row by row. `compute(conn, since=...)` scores the messages received
since then plus every message in a thread touched since then, because the owner's reply changes
whether an older question is still waiting. The "replied" counts are taken over the whole
archive each run; an old message's count may drift until the next full run. Rows whose
values did not change are not rewritten. Change a weight or a rule here → bump VERSION.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg

from talos import personal, rules

VERSION = 3

HIGH, MEDIUM, LOW = 70, 45, 10
LEVELS = ("high", "medium", "low", "noise")
OFTEN = 5                   # outgoing messages to a sender that make them someone the owner "often" answers
CONTACT_WINDOW = "2 years"
BROADCAST = 10              # more To+Cc recipients than this and a mail is not "direct"
SMALL_CHAT = 5              # other members in a Teams chat that still count as direct
DATE_HORIZON_DAYS = 14      # a date this many days after the message counts as a deadline
TEXT_CHARS = 4000           # of the quote-stripped text, read for questions and deadlines

NOISE_TYPES = ["newsletter", "notification", "alert", "backup"]
NOISE_TOPICS = ["newsletters"]

# A no-reply-style local part, as in mime._NOREPLY; checked again here so a sender that
# slipped past the parser's automated flag is still not a person.
NOREPLY = (r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|notifications?|notify|alerts?|"
           r"mailer-daemon|postmaster|bounces?|automated|robot|news(letter)?|marketing|quarantine|"
           r"reports?|digest|updates?)([+-].*)?$")
# Calendar replies and notices ("Accepted: …", "Inställt: …") are machine-made, not questions.
MEETING_REPLY = (r"^\s*(accepted|declined|tentative|tentatively accepted|canceled|cancelled|updated invitation|"
                 r"invitation|new time proposed|accepterat|accepterad|avböjt|avböjd|preliminärt|preliminär|inställt|inställd|"
                 r"uppdaterad inbjudan|inbjudan|nytt tidsförslag|ny tid föreslagen)\s*:")
# A question to the owner: a question mark, or Swedish/English asking phrases. URLs and addresses
# are removed from the text first (a URL's "?" is not a question).
QUESTION = (r"\?|\m(kan du|kan ni|skulle du|skulle ni|vill du|vill ni|har du möjlighet|har ni möjlighet|"
            r"hinner du|hinner ni|har du tid|vad tycker du|vad tycker ni|återkom|ge besked|"
            r"could you|can you|would you|will you|are you able|do you have time|let me know|"
            r"what do you think|please (confirm|advise|review|reply|respond))\M")
DEADLINE = (r"\m(senast|deadline|innan|före|snarast|brådskande|asap|urgent|due (date|by)|"
            r"by (monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|eod|end of (the )?(day|week)))\M")
# ISO dates, Swedish day/month, and "30 sep" / "30:e september".
DATES = (r"\m(?:(20\d\d)-(\d\d)-(\d\d)|([0-3]?\d)/([01]?\d)|([0-3]?\d)(?::[ae])?\.? "
         r"(jan|feb|mar|apr|maj|may|jun|jul|aug|sep|okt|oct|nov|dec)[a-z]*)\M")


@dataclass(frozen=True)
class Signal:
    key: str
    points: int
    when: str   # SQL over f (features), x (text features)
    text: str   # SQL expression giving the explanation shown in the UI
    noise: bool = False


QUESTION_ASKED = "coalesce(x.question, false) and f.direct and f.person and f.known and not f.meeting_reply"
SIGNALS = [
    Signal("person", 20, "f.person", "'Written by a person'"),
    Signal("direct", 20, "f.direct",
           "case when f.medium = 'teams_chat' then 'A chat with you and few others' else 'Sent to you directly' end"),
    Signal("copied", 5, "f.copied",
           "case when f.medium = 'teams_chat' then 'A group chat you are in' else 'You are on Cc or one of many' end"),
    Signal("replied_often", 25, f"f.contact_n >= {OFTEN}",
           "format('You have written to them %s times in two years', f.contact_n)"),
    Signal("replied", 15, f"f.contact_n between 1 and {OFTEN - 1}",
           "case when f.contact_n = 1 then 'You have written to them before'"
           " else format('You have written to them %s times in two years', f.contact_n) end"),
    Signal("in_thread", 15, "f.in_thread and f.medium <> 'teams_chat'", "'A thread you have written in'"),
    Signal("question", 20, f"{QUESTION_ASKED} and not f.answered", "'Asks you something, and you have not answered'"),
    Signal("answered", 0, f"{QUESTION_ASKED} and f.answered", "'Asked you something; you have answered since'"),
    Signal("deadline", 10, "coalesce(x.deadline, false)", "'Mentions a deadline or a date in the coming days'"),
    Signal("vip", 50, "f.vip", "'A VIP sender'"),
    Signal("filed_noise", -60, "f.noise_value is not null", "format('Filed as %s', f.noise_value)", noise=True),
    Signal("list", -40, "f.list", "'Sent to a mailing list'", noise=True),
    Signal("automated", -30, "f.automated", "'Sent by a machine'", noise=True),
    Signal("meeting_reply", -20, "f.meeting_reply", "'A calendar reply or notice'", noise=True),
]


def _levels_sql() -> str:
    return (f"case when s.marked = 'important' then 'high'"
            f" when s.marked = 'not_important' then 'noise'"
            f" when s.direction <> 'in' then 'low'"
            f" when s.noise_hit then case when s.score >= {LOW} then 'low' else 'noise' end"
            f" when s.score >= {HIGH} then 'high' when s.score >= {MEDIUM} then 'medium'"
            f" when s.score >= {LOW} then 'low' else 'noise' end")


def _scoring_sql() -> str:
    score = " + ".join(f"(case when {s.when} then {s.points} else 0 end)" for s in SIGNALS)
    reasons = ", ".join(
        f"case when {s.when} then jsonb_build_object('signal', '{s.key}', 'points', {s.points}, 'text', {s.text}) end"
        for s in SIGNALS)
    noise = " or ".join(f"({s.when})" for s in SIGNALS if s.noise)
    marked = ("case when f.marked is null then '[]'::jsonb else jsonb_build_array(jsonb_build_object("
              "'signal', 'marked', 'points', 0, 'value', f.marked, 'by', f.marked_by, 'text',"
              " case when f.marked_by = 'human' then 'You marked it ' else 'Marked ' end"
              " || case when f.marked = 'important' then 'important' else 'not important' end"
              " || case when f.marked_by = 'human' then '' else ' (' || f.marked_by || ')' end)) end")
    own = "jsonb_build_array(jsonb_build_object('signal', 'own', 'points', 0, 'text', 'You sent it'))"
    return f"""
        with s as (
            select f.id, f.received_at, f.direction, f.marked,
                   case when f.direction = 'in' then {score} else 0 end as score,
                   case when f.direction = 'in' then to_jsonb(array_remove(array[{reasons}], null)) else {own} end
                       || {marked} as reasons,
                   f.direction = 'in' and ({noise}) as noise_hit,
                   f.direction = 'in' and {QUESTION_ASKED} and not f.answered
                       and f.marked is distinct from 'not_important' as waiting
            from imp_f f left join imp_text x on x.message_id = f.id
        )
        insert into message_importance as i (message_id, score, level, reasons, waiting, received_at, version,
                                             computed_at)
        select s.id, s.score, {_levels_sql()}, s.reasons, s.waiting, s.received_at, {VERSION}, now() from s
        on conflict (message_id) do update set
            score = excluded.score, level = excluded.level, reasons = excluded.reasons, waiting = excluded.waiting,
            received_at = excluded.received_at, version = excluded.version, computed_at = excluded.computed_at
        where (i.score, i.level, i.reasons, i.waiting, i.received_at, i.version)
              is distinct from (excluded.score, excluded.level, excluded.reasons, excluded.waiting,
                                excluded.received_at, excluded.version)
    """


TEMP = ["imp_target", "imp_me", "imp_org", "imp_contact", "imp_thread_out", "imp_rcpt", "imp_value", "imp_f", "imp_text"]


def compute(conn: psycopg.Connection, since: datetime | None = None, *,
            message_ids: list[int] | None = None) -> dict:
    """Score messages: all of them, those received since `since` plus every message in a
    thread touched since then, or exactly `message_ids`. Returns counts and seconds."""
    started = time.monotonic()
    full = since is None and message_ids is None
    with conn.transaction():
        conn.execute("drop table if exists " + ", ".join(TEMP))
        conn.execute("create temp table imp_target (id bigint primary key) on commit drop")
        if message_ids is not None:
            conn.execute("insert into imp_target select distinct unnest(%s::bigint[])"
                         " on conflict do nothing", (list(message_ids),))
            conn.execute("delete from imp_target t where not exists (select 1 from message m where m.id = t.id)")
        elif since is not None:
            conn.execute("insert into imp_target select id from message where received_at >= %(s)s"
                         " union select m.id from thread t join message m on m.thread_id = t.id"
                         " where t.last_at >= %(s)s", {"s": since})
        else:
            conn.execute("insert into imp_target select id from message")
        conn.execute("analyze imp_target")
        ids = None if full else [r["id"] for r in conn.execute("select id from imp_target")]

        conn.execute("create temp table imp_me on commit drop as select distinct lower(address) as address"
                     " from my_address")
        # The owner's own organisations: the domains of their addresses, less the mail providers.
        from talos.ingest import FREEMAIL
        conn.execute("create temp table imp_org on commit drop as select distinct split_part(address, '@', 2) as domain"
                     " from imp_me where split_part(address, '@', 2) <> all(%s)", (sorted(FREEMAIL),))
        # How often the owner writes to each address: the "someone the owner replies to" signal.
        conn.execute(
            "create temp table imp_contact on commit drop as"
            " select p.address, count(distinct p.message_id)::int as n from message o"
            " join participant p on p.message_id = o.id and p.role in ('to', 'cc', 'bcc')"
            f" where o.direction = 'out' and o.received_at >= now() - interval '{CONTACT_WINDOW}'"
            " group by p.address")
        conn.execute("create index on imp_contact (address)")
        conn.execute(
            "create temp table imp_thread_out on commit drop as"
            " select o.thread_id, max(o.received_at) as last_out from message o"
            " where o.direction = 'out' and o.thread_id is not null"
            + ("" if full else " and o.thread_id in (select m.thread_id from imp_target t join message m on m.id = t.id)")
            + " group by o.thread_id")
        conn.execute("create index on imp_thread_out (thread_id)")
        conn.execute(
            "create temp table imp_rcpt on commit drop as"
            " select p.message_id,"
            "   bool_or(p.role = 'to' and me.address is not null) as to_me,"
            "   bool_or(p.role in ('cc', 'bcc') and me.address is not null) as cc_me,"
            "   count(*) filter (where p.role = 'to')::int as n_to,"
            "   count(*) filter (where p.role in ('to', 'cc'))::int as n_rcpt"
            " from imp_target t join participant p on p.message_id = t.id and p.role in ('to', 'cc', 'bcc')"
            " left join imp_me me on me.address = lower(p.address)"
            " group by p.message_id")
        conn.execute("create index on imp_rcpt (message_id)")
        # The effective values (human beats rule …), message and thread together.
        conn.execute(
            "create temp table imp_value on commit drop as"
            " select e.message_id,"
            "   max(e.value) filter (where (e.dimension_id = 'type' and lower(e.value) = any(%(types)s))"
            "                           or (e.dimension_id = 'topic' and lower(e.value) = any(%(topics)s))) as noise_value,"
            "   max(e.value) filter (where e.dimension_id = 'importance') as marked,"
            "   max(e.source_kind) filter (where e.dimension_id = 'importance') as marked_by"
            " from effective_message_assignment e"
            " where e.dimension_id in ('type', 'topic', 'importance')"
            + ("" if full else " and e.message_id = any(%(ids)s::bigint[])")
            + " group by e.message_id",
            {"types": NOISE_TYPES, "topics": NOISE_TOPICS, "ids": ids})
        conn.execute("create index on imp_value (message_id)")
        conn.execute(
            """
            create temp table imp_f on commit drop as
            select m.id, m.direction, m.medium, m.received_at,
                   not m.is_automated and m.from_address is not null
                       and split_part(m.from_address, '@', 1) !~* %(noreply)s as person,
                   (m.list_id is not null or coalesce(m.headers ->> 'list_unsubscribe', '') <> '') as list,
                   m.is_automated as automated,
                   coalesce(r.to_me, false) as to_me, coalesce(r.cc_me, false) as cc_me,
                   coalesce(r.n_to, 0) as n_to, coalesce(r.n_rcpt, 0) as n_rcpt,
                   coalesce(c.n, 0) as contact_n,
                   exists (select 1 from importance_vip v
                           where v.pattern in (lower(m.from_address), split_part(lower(m.from_address), '@', 2))) as vip,
                   o.last_out is not null as in_thread,
                   -- someone the owner knows: written to, in the owner's own organisation, or a VIP
                   (coalesce(c.n, 0) >= 1
                    or split_part(lower(m.from_address), '@', 2) in (select domain from imp_org)
                    or exists (select 1 from importance_vip v2
                               where v2.pattern in (lower(m.from_address), split_part(lower(m.from_address), '@', 2))))
                       as known,
                   coalesce(m.subject, '') ~* %(meeting)s as meeting_reply,
                   coalesce(o.last_out > m.received_at, false) as answered,
                   v.noise_value, v.marked, v.marked_by
            from imp_target t
            join message m on m.id = t.id
            left join imp_rcpt r on r.message_id = m.id
            left join imp_contact c on c.address = lower(m.from_address)
            left join imp_thread_out o on o.thread_id = m.thread_id
            left join imp_value v on v.message_id = m.id
            """, {"noreply": NOREPLY, "meeting": MEETING_REPLY})
        conn.execute(
            f"""
            alter table imp_f add column direct boolean, add column copied boolean;
            update imp_f set
                direct = case when medium = 'teams_chat' then to_me and n_to <= {SMALL_CHAT}
                              when medium = 'email' then to_me and not list and n_rcpt <= {BROADCAST}
                              else false end;
            update imp_f set copied = not direct and not list
                                      and (cc_me or (to_me and medium in ('email', 'teams_chat')));
            create index on imp_f (id);
            analyze imp_f;
            """)
        # Questions and deadlines, read only where they can count: incoming, from a person.
        conn.execute(
            f"""
            create temp table imp_text on commit drop as
            select b.id as message_id,
                   b.body ~* %(question)s as question,
                   b.body ~* %(deadline)s or exists (
                       select 1 from regexp_matches(b.body, %(dates)s, 'gi') d,
                       lateral (select case
                           when d[1] is not null then d[1] || '-' || d[2] || '-' || d[3]
                           when d[4] is not null then extract(year from b.received_at)::int || '-' || d[5] || '-' || d[4]
                           else extract(year from b.received_at)::int || '-'
                                || ((strpos('janfebmaraprmajjunjulaugsepoktnovdec',
                                            replace(replace(lower(d[7]), 'may', 'maj'), 'oct', 'okt')) + 2) / 3)
                                || '-' || d[6] end as s) x
                       where (case when pg_input_is_valid(x.s, 'date') then x.s::date end)
                             between b.received_at::date and b.received_at::date + {DATE_HORIZON_DAYS}
                   ) as deadline
            from (
                select f.id, f.received_at,
                       coalesce(m.subject, '') || E'\\n'
                       || regexp_replace(left(t.quote_stripped, {TEXT_CHARS}), '(https?://|www\\.)\\S+|\\S+@\\S+', ' ', 'g')
                       as body
                from imp_f f
                join message m on m.id = f.id
                join message_text t on t.message_id = f.id
                where f.direction = 'in' and f.person and f.received_at is not null
            ) b
            """, {"question": QUESTION, "deadline": DEADLINE, "dates": DATES})
        conn.execute("create index on imp_text (message_id)")
        changed = conn.execute(_scoring_sql()).rowcount
        levels = {r["level"]: r["n"] for r in conn.execute(
            "select i.level, count(*)::int as n from imp_target t join message_importance i on i.message_id = t.id"
            " group by 1")}
        scored = sum(levels.values())
    return {"scored": scored, "changed": changed, **{lv: levels.get(lv, 0) for lv in LEVELS},
            "seconds": round(time.monotonic() - started, 2), "version": VERSION}


def compute_recent(conn: psycopg.Connection, days: int = 30) -> dict:
    return compute(conn, since=datetime.now(timezone.utc) - timedelta(days=days))


# ---------------------------------------------------------------- the owner's mark and VIPs

MARKS = ("important", "not_important")


def mark(conn: psycopg.Connection, message_id: int, value: str | None, *, who: str = personal.OWNER_ID) -> dict | None:
    """The owner's verdict on one message: 'important', 'not_important', or None to take it back.

    Stored as a human assignment in the importance dimension, so it beats any rule or model,
    and the message is re-scored at once."""
    if value is not None and value not in MARKS:
        raise rules.RuleError(f"importance must be one of {', '.join(MARKS)}, or null")
    if not conn.execute("select 1 from message where id = %s", (message_id,)).fetchone():
        return None
    with conn.transaction():
        if value is None:
            conn.execute("update assignment set status = 'superseded', decided_at = now()"
                         " where entity_id = %s and dimension_id = 'importance' and source_kind = 'human'"
                         " and status = 'active'", (message_id,))
        else:
            rules.assign(conn, [message_id], "importance", value, who=who)
    compute(conn, message_ids=[message_id])
    return detail(conn, message_id)


def vip_list(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("select pattern, note, created_at from importance_vip order by pattern").fetchall()


def _vip_messages(conn: psycopg.Connection, pattern: str) -> list[int]:
    if "@" in pattern:
        rows = conn.execute("select id from message where from_address = %s", (pattern,))
    else:
        rows = conn.execute("select id from message where split_part(from_address, '@', 2) = %s", (pattern,))
    return [r["id"] for r in rows]


def vip_add(conn: psycopg.Connection, pattern: str, note: str | None = None) -> dict:
    """Add a VIP address or domain and re-score its messages."""
    pattern = pattern.strip().lower().lstrip("@")
    if not pattern:
        raise ValueError("a VIP needs an address or a domain")
    with conn.transaction():
        conn.execute("insert into importance_vip (pattern, note) values (%s, %s)"
                     " on conflict (pattern) do update set note = coalesce(excluded.note, importance_vip.note)",
                     (pattern, note))
    return compute(conn, message_ids=_vip_messages(conn, pattern))


def vip_remove(conn: psycopg.Connection, pattern: str) -> dict:
    pattern = pattern.strip().lower().lstrip("@")
    with conn.transaction():
        conn.execute("delete from importance_vip where pattern = %s", (pattern,))
    return compute(conn, message_ids=_vip_messages(conn, pattern))


# ---------------------------------------------------------------- reading, for the API

ITEM = """
    m.id, m.account_id, m.medium, m.thread_id, m.subject, m.from_name, m.from_address, m.received_at, m.snippet,
    i.score, i.level, i.reasons, i.waiting
"""
LEVEL_RANK = "case i.level when 'high' then 3 when 'medium' then 2 when 'low' then 1 else 0 end"


def detail(conn: psycopg.Connection, message_id: int) -> dict | None:
    """The drawer's view: score, level, reasons, waiting, and the owner's own mark (or null)."""
    row = conn.execute("select score, level, reasons, waiting, version, computed_at from message_importance"
                       " where message_id = %s", (message_id,)).fetchone()
    marked = conn.execute("select value from effective_message_assignment where message_id = %s"
                          " and dimension_id = 'importance' and source_kind = 'human'", (message_id,)).fetchone()
    if not row:
        return None
    return {**row, "marked": marked["value"] if marked else None}


def today(conn: psycopg.Connection, *, limit: int = 5, hours: int = 24) -> list[dict]:
    """The most important incoming messages of the last `hours`: high and medium only,
    best first (level, then score, then newest)."""
    # One item per conversation: a busy chat or thread shows once, as its best message.
    return conn.execute(
        f"select * from (select distinct on (coalesce(m.thread_id, -m.id)) {ITEM}, {LEVEL_RANK} as rank"
        " from message_importance i join message m on m.id = i.message_id"
        " where i.level in ('high', 'medium') and i.received_at >= now() - make_interval(hours => %s)"
        " and m.direction = 'in'"
        f" order by coalesce(m.thread_id, -m.id), {LEVEL_RANK} desc, i.score desc, i.received_at desc) x"
        " order by rank desc, score desc, received_at desc limit %s",
        (hours, limit)).fetchall()


def waiting(conn: psycopg.Connection, *, limit: int = 10, days: int = 30) -> list[dict]:
    """Important questions to the owner, still unanswered, received in the last `days`; oldest first."""
    # One item per conversation (its oldest unanswered question), oldest first.
    return conn.execute(
        "select * from (select distinct on (coalesce(m.thread_id, -m.id))"
        f" {ITEM}, extract(epoch from now() - m.received_at)::bigint as waited_seconds,"
        " round((extract(epoch from now() - m.received_at) / 86400)::numeric, 1) as waited_days"
        " from message_importance i join message m on m.id = i.message_id"
        " where i.waiting and i.level in ('high', 'medium') and i.received_at >= now() - make_interval(days => %s)"
        " order by coalesce(m.thread_id, -m.id), i.received_at, m.id) x"
        " order by received_at, id limit %s", (days, limit)).fetchall()


def _age(seconds: float) -> str:
    hours = seconds / 3600
    if hours < 48:
        n = max(1, round(hours))
        return f"{n} hour{'s' if n != 1 else ''}"
    n = round(hours / 24)
    return f"{n} days"


def check(conn: psycopg.Connection, *, limit: int = 5) -> list[dict]:
    """"Check this, it looks important": a short, deterministic list, one line of reason each.

    1. waiting: important, asks the owner something, unanswered for more than 2 days (last 30 days)
    2. unanswered_contact: someone the owner often writes to wrote to them directly more than a day
       ago (last 14 days), and the owner has not written in the thread since
    3. quiet_thread: a thread with a high message in the last 30 days that has been quiet
       for 5 days or more
    One item per thread; in that order, oldest first within each."""
    rows = conn.execute(
        f"""
        (select 1 as prio, 'waiting' as kind, {ITEM}, null::text as last_direction, null::timestamptz as last_at
         from message_importance i join message m on m.id = i.message_id
         where i.waiting and i.level in ('high', 'medium')
           and i.received_at >= now() - interval '30 days' and i.received_at < now() - interval '2 days'
         order by i.received_at limit 50)
        union all
        (select 2, 'unanswered_contact', {ITEM}, null, null
         from message_importance i join message m on m.id = i.message_id
         where i.received_at >= now() - interval '14 days' and i.received_at < now() - interval '1 day'
           and i.level in ('high', 'medium') and m.direction = 'in'
           and i.reasons @> '[{{"signal": "replied_often"}}]' and i.reasons @> '[{{"signal": "direct"}}]'
           and not exists (select 1 from message o where o.thread_id = m.thread_id and o.direction = 'out'
                           and o.received_at > m.received_at)
         order by i.received_at limit 50)
        union all
        (select distinct on (t.id) 3, 'quiet_thread', {ITEM},
                (select x.direction from message x where x.thread_id = t.id
                 order by x.received_at desc nulls last, x.id desc limit 1),
                t.last_at
         from message_importance i join message m on m.id = i.message_id join thread t on t.id = m.thread_id
         where i.level = 'high' and i.received_at >= now() - interval '30 days'
           and t.last_at < now() - interval '5 days'
         order by t.id, i.score desc, i.received_at desc)
        """).fetchall()
    now = datetime.now(timezone.utc)
    rows.sort(key=lambda r: (r["prio"], r["received_at"] or now))
    out, threads = [], set()
    for r in rows:
        key = r["thread_id"] or -r["id"]
        if key in threads:
            continue
        threads.add(key)
        who = r["from_name"] or r["from_address"] or "Someone"
        if r["kind"] == "waiting":
            reason = f"Asked you something {_age((now - r['received_at']).total_seconds())} ago and has no answer yet"
        elif r["kind"] == "unanswered_contact":
            reason = (f"{who} usually hears back from you; this has waited "
                      f"{_age((now - r['received_at']).total_seconds())}")
        else:
            reason = f"An important thread, quiet for {_age((now - r['last_at']).total_seconds())}" + (
                "; you wrote last" if r["last_direction"] == "out" else "")
        item = {k: v for k, v in r.items() if k not in ("prio", "last_direction", "last_at")}
        item["reason"] = reason
        out.append(item)
        if len(out) >= limit:
            break
    return out
