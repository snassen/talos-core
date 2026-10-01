"""Reading the archive: search with filters, one message in full, conversations (threads: a page of
one, around a message or after it), the raw original, and overview figures.

Shared by the CLI and the web API, so both answer the same question the same way.
Full-text search uses the Swedish and English dictionaries together; an address or
a fragment of a subject also matches literally, since dictionaries do not stem
e-mail addresses.

People and Automated (the `automated` filter) read each message's effective sender_kind (the
boundary, talos.boundary), then its effective origin (talos.enrich: the person values are people,
the machine values automated); a message with neither falls back to its is_automated flag. See
machine_sql.

A search never reads the whole archive. The matching ids are gathered first, each branch
by its own index (GIN on message_text.search; trigram GIN on subject and from_address),
and only then joined for filters, ranking and paging. The list columns, which need
subqueries, are computed for the one page shown. A dimension filter finds its candidates
through the assignment value index, then checks each one's effective value.
"""

from __future__ import annotations

import json
import re

import psycopg

from talos import insight, objects, sureness
from talos.enrich import MACHINE_ORIGINS
from talos.vault import Vault

# Where a message is, when it has left the mailbox: the red chip on a row and in the pane. Only the
# server's folders decide (as Talos last saw them), so a message restored from the trash loses it:
# 'purged' - every place the server has it is a trash folder, and a Talos changeset moved it there
#            (a prune);
# 'deleted' - every place is a trash folder, moved there by someone else (Outlook, Spark);
# 'gone'    - an e-mail of a synced account that the server no longer has anywhere.
TRASH_FOLDERS = ("Borttagna objekt", "Deleted Items", "Papperskorgen", "Trash", "Deleted Messages",
                 "[Gmail]/Papperskorgen", "[Gmail]/Trash")
_IN_TRASH = ("(coalesce(l.folder_path, l.folder) in (" + ", ".join(f"'{f}'" for f in TRASH_FOLDERS) + ")"
             " or '\\Trash' = any(l.labels))")
REMOVED_COLUMN = (
    "(select case when count(*) = 0 then case when m.medium = 'email' and m.account_id <> 'local' then 'gone' end"
    f" when bool_and({_IN_TRASH}) then case when exists (select 1 from changeset_op o where o.message_id = m.id"
    " and o.op = 'trash' and o.status = 'done') then 'purged' else 'deleted' end end"
    " from message_location l where l.message_id = m.id and l.present) as removed")

LIST_COLUMNS = """
    m.id, m.account_id, m.medium, m.thread_id, m.received_at, m.direction, m.from_name, m.from_address, m.subject,
    m.snippet, m.is_automated, m.has_attachments,
    m.headers->>'x-teams-chat-type' as chat_type, m.headers->>'x-teams-team' as team, m.headers->>'x-teams-channel' as channel,
    (select array_agg(distinct x) from message_location l, unnest(l.labels) x where l.message_id = m.id and l.present) labels,
    (select array_agg(distinct coalesce(l.folder_path, l.folder)) from message_location l where l.message_id = m.id and l.present) folders,
    (select bool_and('seen' = any(l.flags)) from message_location l where l.message_id = m.id and l.present) seen,
    {removed}
""".format(removed=REMOVED_COLUMN)

# The list's importance marker (talos.importance); null until the message is scored.
IMPORTANCE_COLUMN = ("(select jsonb_build_object('score', mi.score, 'level', mi.level) from message_importance mi"
                     " where mi.message_id = m.id) as importance")


# People / Automated: sender_kind (the boundary, talos.boundary) and origin compete together.
_SENDER_DIMS = "('sender_kind', 'origin')"
_MACHINE = "array[" + ", ".join(f"'{v}'" for v in (*MACHINE_ORIGINS, "machine")) + "]"
# Every entity's own sender_kind and origin values, read from the two partial indexes
# (assignment_sender_kind_idx, assignment_origin_idx).
_OWN_ROWS = ("select a.entity_id, a.value from assignment a where a.dimension_id = 'sender_kind' and a.status = 'active'"
             " union all select a.entity_id, a.value from assignment a where a.dimension_id = 'origin'"
             " and a.status = 'active'")


def _ranked(m: str) -> str:
    """The winning value of the ranking, looked up for one message (by index)."""
    return (f"coalesce((select a.value = any({_MACHINE}) from assignment a"
            f" where a.entity_id = any(array[{m}.id, {m}.thread_id]) and a.dimension_id in {_SENDER_DIMS}"
            f" and a.status = 'active' order by case a.source_kind when 'human' then 4 when 'rule' then 3"
            f" when 'model' then 2 else 1 end desc, (a.dimension_id = 'sender_kind') desc,"
            f" (a.entity_id = {m}.id) desc, a.created_at desc, a.id desc"
            f" limit 1), {m}.is_automated)")


def machine_sql(m: str = "m") -> str:
    """SQL, true when message m was made by a machine, as People / Automated decides it:

    1. its effective **sender_kind** (the boundary of two-level acceptance: machine or people),
    2. else its effective **origin** (the machine values: transactional, notification, marketing,
       alert, …; the people values person, person_via_system and list),
    3. else, with neither, the parser's is_automated flag.

    Both dimensions compete in one ranking, the one effective_message_assignment uses: human >
    rule (and the pre-pass) > accepted model > import, then sender_kind before origin, the
    message's own before its thread's, then the newest. So a human's or a rule's origin still
    beats a model's sender_kind; within a tier the boundary speaks first.

    This form looks each message up on its own: right for one message or a few. A selection
    (the Messages filter, the sender list) uses MACHINE_COLUMN instead, which gives the same
    answer from one grouped pass (messages_sql adds it)."""
    return _ranked(m)


# The same decision for a whole selection. sender_state groups every entity's own values once
# (about half a million rows once sender_kind is accepted: 0.2 s); each message then joins its own
# state and its thread's. Unanimous own values answer at once; a message whose thread carries a
# value, or whose own values disagree, is ranked one by one; one with neither keeps its flag.
SENDER_STATE = (f"sender_state as materialized (select o.entity_id, bool_and(o.value = any({_MACHINE})) as all_m,"
                f" bool_or(o.value = any({_MACHINE})) as any_m from ({_OWN_ROWS}) o group by o.entity_id)")
SENDER_JOINS = (" left join sender_state ss_m on ss_m.entity_id = m.id"
                " left join sender_state ss_t on ss_t.entity_id = m.thread_id")
BOUNDARY_DIMS = ("sender_kind", "sphere", "form", "keep")   # talos.boundary.FIELDS
GROUPED_DIMS = BOUNDARY_DIMS + ("kind",)                     # few values, each on much of the archive
MACHINE_COLUMN = (f"(case when ss_t.entity_id is not null then {_ranked('m')}"
                  f" when ss_m.entity_id is null then m.is_automated"
                  f" when ss_m.all_m then true when not ss_m.any_m then false"
                  f" else {_ranked('m')} end)")


def prefix_query(q: str) -> str | None:
    """A prefix tsquery for plain words, complementing the Swedish stemmer.

    PostgreSQL's Swedish stemmer keeps kvitto, kvittot and kvitton apart (and larm,
    larmet). Trimming a word of five letters or more by two and matching it as a
    prefix joins them: "kvitton" becomes kvitt:*. Queries with quotes or operators
    are left to websearch_to_tsquery alone.
    """
    if any(ch in q for ch in '"-:|&!()'):
        return None
    words = [w.lower() for w in re.findall(r"\w+", q) if not w.isdigit()]
    if not words:
        return None
    parts = [(w[: max(4, len(w) - 2)] if len(w) >= 5 else w) + ":*" for w in words]
    return " & ".join(parts)


# A Teams conversation's kind, as the Teams place filters it: a chat by its chatType, or a channel.
CHAT_KINDS = ("oneOnOne", "group", "meeting", "channel")


def filters_from(get, getlist) -> dict:
    """The Messages filters from query parameters (get(key), getlist(key)): the Messages view's
    own query string, which watchers and aggregations store as it is.

    dim may repeat (dim=type:newsletter&dim=topic:Travel); every pair must hold. accounts,
    exclude_accounts, exclude_senders and exclude_domains repeat the same way."""
    def flag(v):
        return None if v in (None, "") else v in ("1", "true", "yes")

    def many(key):
        return [v.strip() for v in getlist(key) if v.strip()] or None

    dims = [tuple(d.split(":", 1)) for d in getlist("dim") if ":" in d]
    return dict(q=get("q"), account=get("account") or None, direction=get("direction") or None,
                teams_direction=get("teams_direction") if get("teams_direction") in ("in", "out") else None,
                automated=flag(get("automated")), label=get("label") or None,
                has_attachments=flag(get("attachments")), domain=get("domain") or None,
                medium=get("medium") if get("medium") in ("email", "teams") else None,
                chat=get("chat") if get("chat") in CHAT_KINDS else None,
                team=get("team") or None, channel=get("channel") or None,
                thread=int(get("thread")) if get("thread") else None, sender=get("sender") or None,
                dimension=dims or None, important=flag(get("important")) or None,
                sure=get("sure") if get("sure") in sureness.KEYS else None,
                accounts=many("accounts"), exclude_accounts=many("exclude_accounts"),
                exclude_senders=many("exclude_senders"), exclude_domains=many("exclude_domains"))


def filters_from_query(qs: str) -> dict:
    """filters_from for a stored query string."""
    from urllib.parse import parse_qs
    p = parse_qs(qs or "", keep_blank_values=False)
    return filters_from(lambda k: (p.get(k) or [None])[0], lambda k: p.get(k, []))


def messages(conn: psycopg.Connection, q: str | None = None, **filters) -> dict:
    """One page of messages matching a query and filters, and the total that match.

    Filters: account, accounts, exclude_accounts, direction, automated, label, has_attachments,
    domain, thread, sender, exclude_senders, exclude_domains, dimension=(dimension_id, value) or a
    list of pairs, limit, offset."""
    query, params = messages_sql(q, **filters)
    _work_mem(conn)
    rows = conn.execute(query, params).fetchall()
    total = rows[0]["total"] if rows else 0
    for r in rows:
        r.pop("total", None)
    return {"total": total, "rows": rows}


def _work_mem(conn: psycopg.Connection) -> None:
    """People / Automated and the boundary filters group a few hundred thousand values per query;
    with room for them in memory they neither sort on disk nor spill (0.6 s → 0.4 s on the real
    archive). Only inside a transaction (the web's connections); an autocommit one keeps its own."""
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE or not conn.autocommit:
        conn.execute("set local work_mem = '64MB'")


def messages_sql(q: str | None = None, *, account: str | None = None, direction: str | None = None,
                 teams_direction: str | None = None,
                 automated: bool | None = None, label: str | None = None, has_attachments: bool | None = None,
                 domain: str | None = None, thread: int | None = None,
                 dimension: tuple[str, str] | list[tuple[str, str]] | None = None,
                 medium: str | None = None, chat: str | None = None, team: str | None = None,
                 channel: str | None = None, limit: int = 50, offset: int = 0,
                 sender: str | None = None, select: str | None = None,
                 important: bool | None = None, sort: str | None = None,
                 accounts: list[str] | None = None, exclude_accounts: list[str] | None = None,
                 exclude_senders: list[str] | None = None,
                 exclude_domains: list[str] | None = None, with_machine: bool = False,
                 sure: str | None = None) -> tuple[str, dict]:
    """The SQL behind messages(), separate so its plan can be checked with EXPLAIN.

    dimension is one (dimension_id, value) pair or a list of them, all required. With
    select, the answer is not a page but "select <select> from the matching messages m",
    unordered and unpaged, for the sender list and the facets (talos.facets).
    important=True keeps high and medium messages; sort='importance' orders by level, then
    score (talos.importance).

    accounts keeps only those accounts; exclude_accounts, exclude_senders and exclude_domains
    leave those out (the Messages view's account chips and hidden senders). An empty list is
    no filter.

    with_machine joins what People / Automated needs (sender_state), so a select can use
    MACHINE_COLUMN; the automated filter joins it too.

    direction applies to mail only, teams_direction to Teams only: in a chat both sides are the conversation,
    so Teams shows both ways unless teams_direction narrows it.

    sure (a level of talos.sureness) makes the dimension filters ask how sure: a value counts only
    at that level or surer (the owner's own is Fact, a rule's Certain, Jev's by its confidence), and where
    a message's field has no value that counts yet, Jev's proposal counts when it is that sure. So
    "kind: alert, Maybe or surer" also finds the alerts Jev guessed but Talos has not accepted."""
    sure = sureness.check(sure)
    where, params = ["true"], {}
    rank, source = "0", "message m"
    if q and q.strip():
        params["q"] = q.strip()
        params["like"] = f"%{q.strip()}%"
        prefix = prefix_query(q)
        params["prefix"] = prefix or ""
        # One branch per index; UNION removes the duplicates. Semantics as before: a message
        # matches the full text (either dictionary, or the prefix form), or its sender
        # address or subject contains the query literally.
        hits = ("select x.message_id as id from message_text x"
                " where x.search @@ (websearch_to_tsquery('swedish', %(q)s) || websearch_to_tsquery('english', %(q)s))"
                + (" or x.search @@ to_tsquery('simple', %(prefix)s)" if prefix else "")
                + " union select x.id from message x where x.subject ilike %(like)s"
                  " union select x.id from message x where x.from_address ilike %(like)s")
        source = f"({hits}) h join message m on m.id = h.id"
        rank = "ts_rank(t.search, websearch_to_tsquery('swedish', %(q)s) || websearch_to_tsquery('english', %(q)s))"
    if medium == "email":
        where.append("m.medium = 'email'")
    elif medium == "teams":
        where.append("m.medium in ('teams_chat', 'teams_channel')")
    # Teams: a chat of one kind (one-to-one, group, meeting) or channel threads, a team, a channel.
    if chat == "channel":
        where.append("m.medium = 'teams_channel'")
    elif chat:
        where.append("m.medium = 'teams_chat' and m.headers->>'x-teams-chat-type' = %(chat)s")
        params["chat"] = chat
    if team:
        where.append("m.medium = 'teams_channel' and m.headers->>'x-teams-team' = %(team)s")
        params["team"] = team
    if channel:
        where.append("m.medium = 'teams_channel' and m.headers->>'x-teams-channel' = %(channel)s")
        params["channel"] = channel
    if account:
        where.append("m.account_id = %(account)s")
        params["account"] = account
    if accounts:
        where.append("m.account_id = any(%(accounts)s)")
        params["accounts"] = list(accounts)
    if exclude_accounts:
        where.append("m.account_id <> all(%(x_accounts)s)")
        params["x_accounts"] = list(exclude_accounts)
    if exclude_senders:
        where.append("coalesce(m.from_address, '') <> all(%(x_senders)s)")
        params["x_senders"] = [s.strip().lower() for s in exclude_senders]
    if exclude_domains:
        where.append("split_part(coalesce(m.from_address, ''), '@', 2) <> all(%(x_domains)s)")
        params["x_domains"] = [d.strip().lower() for d in exclude_domains]
    if direction or teams_direction:
        # Mail from one of the owner's addresses to another ('self', e.g. Gmail to work) is both
        # received and sent, so Received and Sent each include it. direction narrows mail and
        # teams_direction Teams; either side without one keeps both ways.
        both = lambda d: [d, "self"] if d in ("in", "out") else [d]
        mail = "m.direction = any(%(direction)s)" if direction else "true"
        chat = "m.direction = any(%(t_direction)s)" if teams_direction else "true"
        where.append(f"((m.medium = 'email' and {mail}) or (m.medium <> 'email' and {chat}))")
        if direction:
            params["direction"] = both(direction)
        if teams_direction:
            params["t_direction"] = both(teams_direction)
    machine = automated is not None or with_machine
    ctes = [SENDER_STATE] if machine else []
    if machine:
        source += SENDER_JOINS
    if automated is not None:
        # People / Automated: by sender_kind, then origin, then the automated flag.
        where.append(f"{MACHINE_COLUMN} = %(automated)s")
        params["automated"] = automated
    if has_attachments is not None:
        where.append("m.has_attachments = %(has_att)s")
        params["has_att"] = has_attachments
    if domain:
        where.append("split_part(m.from_address, '@', 2) = %(domain)s")
        params["domain"] = domain.lower()
    if thread:
        where.append("m.thread_id = %(thread)s")
        params["thread"] = thread
    if label:
        where.append("exists (select 1 from message_location l where l.message_id = m.id and l.present"
                     " and %(label)s = any(l.labels))")
        params["label"] = label
    for i, (dim_id, dim_value) in enumerate([dimension] if isinstance(dimension, tuple) else dimension or []):
        params[f"dim{i}"], params[f"dimval{i}"] = dim_id, dim_value
        if sure:
            # How sure (talos.sureness): candidates carry the value, active or proposed, themselves or
            # (active) on their thread; each keeps it when the value that counts is as sure as asked,
            # or, with nothing counting for the field, Jev's proposal is.
            where.append(
                f"m.id in (select a.entity_id from assignment a where a.status in ('active', 'proposed')"
                f" and a.dimension_id = %(dim{i})s and a.value = %(dimval{i})s"
                f" union select x.id from assignment a join message x on x.thread_id = a.entity_id"
                f" where a.status = 'active' and a.dimension_id = %(dim{i})s and a.value = %(dimval{i})s)"
                f" and (exists (select 1 from effective_message_assignment e where e.message_id = m.id"
                f" and e.dimension_id = %(dim{i})s and e.value = %(dimval{i})s"
                f" and {sureness.floor_sql(sure, 'e.source_kind', 'e.confidence')})"
                f" or (not exists (select 1 from effective_message_assignment e where e.message_id = m.id"
                f" and e.dimension_id = %(dim{i})s)"
                f" and exists (select 1 from assignment p where p.entity_id = m.id and p.dimension_id = %(dim{i})s"
                f" and p.value = %(dimval{i})s and p.status = 'proposed' and p.source_kind = 'model'"
                f" and {sureness.floor_sql(sure, chr(39) + 'model' + chr(39), 'p.confidence')})))")
            continue
        if dim_id in GROUPED_DIMS:
            # A boundary (or kind) holds one of a few values on most messages: a candidate list would be half
            # the archive. So, as for People / Automated, the dimension's values are grouped once
            # per entity (assignment_boundary_idx, in entity order) and joined: unanimous own values
            # with nothing on the thread answer at once, anything else is ranked by the view.
            ctes.append(f"ds{i} as materialized (select a.entity_id, bool_and(a.value = %(dimval{i})s) as all_x,"
                        f" bool_or(a.value = %(dimval{i})s) as any_x from assignment a"
                        f" where a.dimension_id = %(dim{i})s and a.status = 'active' group by a.entity_id)")
            source += (f" left join ds{i} on ds{i}.entity_id = m.id"
                       f" left join ds{i} dt{i} on dt{i}.entity_id = m.thread_id")
            where.append(
                f"(case when dt{i}.entity_id is null and ds{i}.entity_id is null then false"
                f" when dt{i}.entity_id is null and ds{i}.all_x then true"
                f" when dt{i}.entity_id is null and not ds{i}.any_x then false"
                f" else coalesce((select true from effective_message_assignment e where e.message_id = m.id"
                f" and e.dimension_id = %(dim{i})s and e.value = %(dimval{i})s limit 1), false) end)")
            continue
        # Candidates: messages that carry the value themselves, or whose thread does; both
        # found by index (assignment_value_idx, then message_thread_idx). Each candidate is
        # then checked against its effective value, which may be a different, stronger one.
        where.append(
            f"m.id in (select a.entity_id from assignment a where a.status = 'active'"
            f" and a.dimension_id = %(dim{i})s and a.value = %(dimval{i})s"
            f" union select x.id from assignment a join thread th on th.id = a.entity_id"
            f" join message x on x.thread_id = th.id"
            f" where a.status = 'active' and a.dimension_id = %(dim{i})s and a.value = %(dimval{i})s)"
            f" and coalesce((select true from effective_message_assignment e where e.message_id = m.id"
            f" and e.dimension_id = %(dim{i})s and e.value = %(dimval{i})s limit 1), false)")
    # -- messages-rules: the sender filter, and the unpaged form used by talos.facets.
    if sender:
        where.append("m.from_address = %(sender)s")
        params["sender"] = sender.strip().lower()
    # importance filter first, so the sender list and the facets (select=…) follow it too
    if important:
        where.append("m.id in (select mi.message_id from message_importance mi where mi.level in ('high', 'medium'))")
    cte = f"with {', '.join(ctes)} " if ctes else ""
    if select:
        return f"{cte}select {select} from {source} where {' and '.join(where)}", params
    # -- end messages-rules
    params["limit"], params["offset"] = min(limit, 500), offset
    order = "{p}rank desc, {p}received_at desc nulls last, {p}id desc" if q else \
            "{p}received_at desc nulls last, {p}id desc"
    # ---- importance (talos.importance): an optional filter and order, kept apart from the rest.
    imp_join, imp_cols = "", "0 as imp_rank, 0 as imp_score"
    if sort == "importance":
        imp_join = " left join message_importance i on i.message_id = m.id"
        imp_cols = ("case i.level when 'high' then 3 when 'medium' then 2 when 'low' then 1 else 0 end as imp_rank,"
                    " coalesce(i.score, -1000) as imp_score")
        order = "{p}imp_rank desc, {p}imp_score desc, " + order
    # ---- end importance
    return (f"{cte.rstrip() + ',' if cte else 'with'} page as (select m.id, m.received_at, {rank} as rank, {imp_cols},"
            f" count(*) over () as total"
            f" from {source} left join message_text t on t.message_id = m.id{imp_join} where {' and '.join(where)}"
            f" order by {order.format(p='')} limit %(limit)s offset %(offset)s)"
            f" select {LIST_COLUMNS}, {IMPORTANCE_COLUMN}, p.rank, p.total from page p join message m on m.id = p.id"
            f" order by {order.format(p='p.')}", params)


def message(conn: psycopg.Connection, message_id: int) -> dict | None:
    m = conn.execute(f"select {LIST_COLUMNS}, m.sent_at, m.rfc_message_id, m.list_id, m.headers, m.raw_sha256,"
                     " t.body_text, t.quote_stripped, t.body_kind from message m"
                     " left join message_text t on t.message_id = m.id where m.id = %s", (message_id,)).fetchone()
    if not m:
        return None
    m["participants"] = conn.execute(
        "select role, address, name from participant where message_id = %s order by role, ordinal", (message_id,)).fetchall()
    m["attachments"] = conn.execute(
        "select id, filename, content_type, size_bytes, extract_status, attrs, left(extracted_text, 600) preview"
        " from attachment where message_id = %s order by part_path", (message_id,)).fetchall()
    # The values that count (solid tags in the pane), and what a model proposed that does not count
    # yet (dashed tags): the proposals of the latest run in the chain (older ones are superseded),
    # left out where the field already has that value, or any value for a one-value field.
    m["values"] = conn.execute(
        "select e.dimension_id, e.value, e.source_kind, e.source_ref, e.via, e.confidence,"
        " coalesce(d.value_meta -> e.value ->> 'label', e.value) as label, d.label as dimension_label"
        " from effective_message_assignment e join dimension d on d.id = e.dimension_id"
        " where e.message_id = %s order by e.dimension_id, e.value", (message_id,)).fetchall()
    m["proposals"] = conn.execute(
        "select a.dimension_id, a.value, a.confidence, a.source_ref,"
        " coalesce(d.value_meta -> a.value ->> 'label', a.value) as label, d.label as dimension_label"
        " from assignment a join dimension d on d.id = a.dimension_id"
        " where a.entity_id = %(m)s and a.source_kind = 'model' and a.status = 'proposed'"
        " and not exists (select 1 from effective_message_assignment e where e.message_id = %(m)s"
        "  and e.dimension_id = a.dimension_id and (d.cardinality = 'one' or e.value = a.value))"
        " order by a.dimension_id, a.confidence desc nulls last, a.value", {"m": message_id}).fetchall()
    for v in m["values"]:  # how sure each is, in words (talos.sureness)
        v["level"] = sureness.level_of(v["source_kind"], v["confidence"])
    for v in m["proposals"]:
        v["level"] = sureness.level_of("model", v["confidence"])
    if m.get("removed") == "purged":
        m["purged_by"] = conn.execute(
            "select o.changeset_id, o.applied_at, c.title from changeset_op o join changeset c on c.id = o.changeset_id"
            " where o.message_id = %s and o.op = 'trash' and o.status = 'done' order by o.applied_at desc nulls last limit 1",
            (message_id,)).fetchone()
    m["events"] = conn.execute("select kind, status, system from event where message_id = %s", (message_id,)).fetchall()
    m["objects"] = objects.objects_of(conn, message_id)
    m["thread"] = conn.execute(
        "select id, received_at, direction, from_name, from_address, snippet from message where thread_id = %s"
        " order by received_at nulls first", (m["thread_id"],)).fetchall() if m["thread_id"] else []
    return m


# ---------------------------------------------------------------- conversations
#
# A conversation is a thread: an e-mail thread, or a Teams chat (or a channel's reply chain).
# The Conversations list takes the Messages filters as they are: a thread matches when any of
# its messages matches, and the list is ordered by the thread's latest activity, whatever
# matched. The matching thread ids come from messages_sql's unpaged form, so a filter means
# exactly what it means for the Messages list; only the page shown is described in full.

THREAD_MAX_LIMIT = 200
THREAD_DETAIL_MAX = 500
_ME = "m.direction in ('out', 'self')"
_LEVEL_RANK = "case mi.level when 'high' then 3 when 'medium' then 2 when 'low' then 1 when 'noise' then 0 end"
_LEVELS = "(array['noise', 'low', 'medium', 'high'])"


def threads(conn: psycopg.Connection, q: str | None = None, **filters) -> dict:
    """One page of conversations matching the Messages filters, newest activity first, and the
    number that match. Takes the filters of messages(), with limit and offset."""
    query, params = threads_sql(q, **filters)
    _work_mem(conn)
    rows = conn.execute(query, params).fetchall()
    total = rows[0]["total"] if rows else 0
    for r in rows:
        r.pop("total", None)
    return {"total": total, "rows": rows}


def threads_sql(q: str | None = None, *, limit: int = 50, offset: int = 0, **filters) -> tuple[str, dict]:
    """The SQL behind threads(), separate so its plan can be checked with EXPLAIN.

    Per thread on the page: the latest message (who, what, when), the people who wrote in it
    (each once, the owner as me, in order of their first message), how many messages it has, how
    many are unread and how many are the owner's, whether any has attachments, and the highest importance
    level among them."""
    for k in ("select", "sort"):
        filters.pop(k, None)
    inner, params = messages_sql(q, select="m.thread_id", **filters)
    params["t_limit"], params["t_offset"] = max(1, min(limit, THREAD_MAX_LIMIT)), max(0, offset)
    return (f"with hits as (select thread_id as id, count(*) as matched from ({inner}) x"
            f"               where thread_id is not null group by 1),"
            f" page as (select h.id, h.matched, t.last_at, count(*) over () as total from hits h"
            f"          join thread t on t.id = h.id order by t.last_at desc nulls last, t.id desc"
            f"          limit %(t_limit)s offset %(t_offset)s)"
            f" select t.id, t.account_id, t.subject, t.first_at, t.last_at, t.message_count, p.matched, p.total,"
            f"  l.id as latest_id, l.medium, l.direction, l.from_name, l.from_address, l.snippet,"
            f"  l.received_at, l.headers->>'x-teams-chat-type' as chat_type,"
            f"  l.headers->>'x-teams-team' as team, l.headers->>'x-teams-channel' as channel,"
            f"  (select array_agg(coalesce(nullif(pt.name, ''), pt.address) order by pt.ordinal) from participant pt"
            f"   where pt.message_id = l.id and pt.role = 'to') as recipients,"
            f"  s.unread, s.mine, s.attachments, s.automated, s.level, w.people, w.people_count"
            f" from page p join thread t on t.id = p.id"
            f" cross join lateral (select m.* from message m where m.thread_id = t.id"
            f"   order by m.received_at desc nulls last, m.id desc limit 1) l"
            f" cross join lateral (select"
            f"   count(*) filter (where exists (select 1 from message_location ml where ml.message_id = m.id"
            f"     and ml.present and not ('seen' = any(ml.flags)))) as unread,"
            f"   count(*) filter (where {_ME}) as mine, bool_or(m.has_attachments) as attachments,"
            f"   bool_and(m.is_automated) as automated,"
            f"   {_LEVELS}[1 + max({_LEVEL_RANK})] as level"
            f"   from message m left join message_importance mi on mi.message_id = m.id where m.thread_id = t.id) s"
            f" cross join lateral (select jsonb_agg(jsonb_build_object('name', k.name, 'address', k.address, 'me', k.me)"
            f"     order by k.first) filter (where k.rn <= 6) as people, count(*) as people_count from ("
            f"   select k0.*, row_number() over (order by k0.first) as rn from ("
            f"     select case when {_ME} then '' else lower(coalesce(m.from_address, m.from_name, '?')) end as key,"
            f"       bool_or({_ME}) as me, any_value(nullif(m.from_name, '')) as name,"
            f"       any_value(m.from_address) as address, min(m.received_at) as first"
            f"     from message m where m.thread_id = t.id group by 1) k0) k) w"
            f" order by p.last_at desc nulls last, p.id desc", params)


def thread(conn: psycopg.Connection, thread_id: int, *, before: int | None = None, after: int | None = None,
           around: int | None = None, only: list[int] | None = None, limit: int = 100) -> dict | None:
    """A conversation: its messages oldest first, the people in it, and how to page back.

    The page is the `limit` newest messages, or with before=<message id> the `limit` messages
    just older than that one; has_more says whether older ones remain. around=<message id> is the
    page with that message in its middle (a found message opened where it was said), after=<id> the
    `limit` messages just newer than that one; has_later says whether newer ones remain. Teams
    messages carry their text (a chat shows every line); an e-mail's text is fetched when it is
    opened. Every message carries its direction, so the owner's own are known."""
    if around:
        return _thread_around(conn, thread_id, around, limit)
    if after:
        return _thread_after(conn, thread_id, after, limit)
    t = conn.execute("select id, account_id, provider_thread_id, subject, first_at, last_at, message_count"
                     " from thread where id = %s", (thread_id,)).fetchone()
    if not t:
        return None
    limit = max(1, min(limit, THREAD_DETAIL_MAX))
    cursor = ""
    params: dict = {"t": thread_id, "limit": limit + 1}
    if only is not None:
        cursor = " and m.id = any(%(only)s)"
        params["only"] = list(only)
    elif before:
        cursor = (" and (coalesce(m.received_at, '-infinity'::timestamptz), m.id) <"
                  " (select coalesce(b.received_at, '-infinity'::timestamptz), b.id from message b"
                  "  where b.id = %(before)s and b.thread_id = %(t)s)")
        params["before"] = before
    rows = conn.execute(
        "select m.id, m.medium, m.received_at, m.direction, m.from_name, m.from_address, m.subject, m.snippet,"
        " m.has_attachments, m.is_automated,"
        " case when m.medium <> 'email' then x.body_text end as text,"
        " m.headers->>'x-teams-edited' as edited, m.headers->>'x-teams-deleted' as deleted,"
        " m.headers->>'x-teams-importance' as teams_importance,"
        " (select bool_and('seen' = any(l.flags)) from message_location l where l.message_id = m.id and l.present) seen,"
        " (select jsonb_build_object('score', mi.score, 'level', mi.level) from message_importance mi"
        "  where mi.message_id = m.id) as importance"
        " from message m left join message_text x on x.message_id = m.id"
        f" where m.thread_id = %(t)s{cursor}"
        " order by coalesce(m.received_at, '-infinity'::timestamptz) desc, m.id desc limit %(limit)s", params).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit][::-1]
    atts: dict[int, list] = {}
    if rows:
        for a in conn.execute(
                "select id, message_id, filename, content_type, size_bytes, attrs from attachment"
                " where message_id = any(%s) order by message_id, part_path", ([r["id"] for r in rows],)):
            atts.setdefault(a.pop("message_id"), []).append(a)
    for r in rows:
        r["attachments"] = atts.get(r["id"], [])
    latest = conn.execute("select id, medium, headers->>'x-teams-chat-type' as chat_type,"
                          " headers->>'x-teams-team' as team, headers->>'x-teams-channel' as channel from message"
                          " where thread_id = %s order by received_at desc nulls last, id desc limit 1",
                          (thread_id,)).fetchone()
    # The people: everyone who wrote (with how many messages), then the latest message's other
    # recipients (a chat's members who have not written, the Cc of a mail).
    people = conn.execute(
        "select case when m.direction in ('out', 'self') then '' else lower(coalesce(m.from_address, m.from_name, '?'))"
        " end as key, bool_or(m.direction in ('out', 'self')) as me, any_value(nullif(m.from_name, '')) as name,"
        " any_value(m.from_address) as address, count(*) as n, min(m.received_at) as first"
        " from message m where m.thread_id = %s group by 1 order by first", (thread_id,)).fetchall()
    seen = {p["key"] for p in people}
    if latest:
        for p in conn.execute(
                "select lower(p.address) as key, p.address, nullif(p.name, '') as name,"
                " exists (select 1 from my_address a where a.address = lower(p.address)) as me"
                " from participant p where p.message_id = %s and p.role in ('from', 'to', 'cc')"
                " order by p.role desc, p.ordinal", (latest["id"],)):
            key = "" if p["me"] else p["key"]
            if key not in seen:
                seen.add(key)
                people.append({**p, "key": key, "n": 0, "first": None})
    for p in people:
        p.pop("key", None)
        p.pop("first", None)
    return {**t, "medium": latest["medium"] if latest else "email", "chat_type": latest["chat_type"] if latest else None,
            "team": latest["team"] if latest else None, "channel": latest["channel"] if latest else None,
            "people": people, "messages": rows, "has_more": has_more,
            "before": rows[0]["id"] if rows and has_more else None, "has_later": False, "after": None}


def _thread_page(conn: psycopg.Connection, thread_id: int, *, older_than: int | None = None, newer_than: int | None = None,
                 including: bool = False, limit: int) -> list[int]:
    """Message ids of one page, oldest first: the limit+1 just older (or newer) than a message."""
    key = "(coalesce(m.received_at, '-infinity'::timestamptz), m.id)"
    ref = ("(select coalesce(b.received_at, '-infinity'::timestamptz), b.id from message b"
           " where b.id = %(ref)s and b.thread_id = %(t)s)")
    if older_than is not None:
        cond, order, ref_id = f"{key} {'<=' if including else '<'} {ref}", "desc", older_than
    else:
        cond, order, ref_id = f"{key} {'>=' if including else '>'} {ref}", "asc", newer_than
    rows = conn.execute(f"select m.id from message m where m.thread_id = %(t)s and {cond}"
                        f" order by coalesce(m.received_at, '-infinity'::timestamptz) {order}, m.id {order} limit %(n)s",
                        {"t": thread_id, "ref": ref_id, "n": limit + 1}).fetchall()
    return [r["id"] for r in rows]


def _thread_with(conn: psycopg.Connection, thread_id: int, ids: list[int], *, has_more: bool, has_later: bool) -> dict | None:
    """The conversation as thread() gives it, with exactly these messages (a window), and its paging."""
    out = thread(conn, thread_id, only=ids, limit=max(1, len(ids)))
    if out is None:
        return None
    rows = out["messages"]
    return {**out, "has_more": has_more, "before": rows[0]["id"] if rows and has_more else None,
            "has_later": has_later, "after": rows[-1]["id"] if rows and has_later else None}


def _thread_around(conn: psycopg.Connection, thread_id: int, around: int, limit: int) -> dict | None:
    half = max(1, min(limit, THREAD_DETAIL_MAX) // 2)
    older = _thread_page(conn, thread_id, older_than=around, limit=half)
    newer = _thread_page(conn, thread_id, newer_than=around, including=True, limit=half + 1)  # the message and half after it
    if not newer:  # not in this conversation: the newest page instead
        return thread(conn, thread_id, limit=limit)
    ids = list(reversed(older[:half])) + newer[:half + 1]
    out = _thread_with(conn, thread_id, ids, has_more=len(older) > half, has_later=len(newer) > half + 1)
    if out is not None:
        out["around"] = around
    return out


def _thread_after(conn: psycopg.Connection, thread_id: int, after: int, limit: int) -> dict | None:
    limit = max(1, min(limit, THREAD_DETAIL_MAX))
    newer = _thread_page(conn, thread_id, newer_than=after, limit=limit)
    return _thread_with(conn, thread_id, newer[:limit], has_more=True, has_later=len(newer) > limit)


def raw(conn: psycopg.Connection, vault: Vault, message_id: int) -> bytes | None:
    found = original(conn, vault, message_id)
    return found[0] if found else None


def original(conn: psycopg.Connection, vault: Vault, message_id: int) -> tuple[bytes, str] | None:
    """The message's original and its blob kind: 'raw' (.eml) for mail, 'json' for Teams."""
    row = conn.execute("select m.raw_sha256, b.kind from message m join blob b on b.sha256 = m.raw_sha256"
                       " where m.id = %s", (message_id,)).fetchone()
    return (vault.get(row["raw_sha256"], row["kind"]), row["kind"]) if row else None


def overview(conn: psycopg.Connection) -> dict:
    one = conn.execute(
        "select (select count(*) from message) messages, (select count(*) from thread) threads,"
        " (select count(*) from attachment) attachments, (select count(*) from person) people,"
        " (select count(*) from org) orgs, (select count(*) from event) events,"
        " (select count(*) from message where is_automated) automated,"
        " (select count(*) from message where direction = 'out') sent,"
        " (select coalesce(sum(stored_size), 0) from blob) vault_bytes").fetchone()
    one["accounts"] = conn.execute(
        "select a.id, a.display_name, a.provider, a.enabled, count(m.id) messages,"
        " max(m.received_at) latest,"
        " (select max(finished_at) from sync_run r where r.account_id = a.id and r.status in ('ok', 'partial')) last_sync"
        " from account a left join message m on m.account_id = a.id group by a.id order by a.id").fetchall()
    one["monthly"] = conn.execute(
        "select to_char(date_trunc('month', received_at), 'YYYY-MM') as month, account_id, count(*) n"
        " from message where received_at >= date_trunc('month', now()) - interval '23 months'"
        " group by 1, 2 order by 1").fetchall()
    one["top_domains"] = conn.execute(
        "select split_part(from_address, '@', 2) as domain, count(*) n, bool_or(is_automated) automated"
        " from message where direction = 'in' group by 1 order by 2 desc limit 15").fetchall()
    one["attachment_types"] = conn.execute(
        "select content_type, count(*) n, sum(size_bytes) bytes from attachment group by 1 order by 2 desc limit 10"
    ).fetchall()
    one["event_breakdown"] = conn.execute(
        "select kind, status, count(*) n from event group by 1, 2 order by 1, 2").fetchall()
    one["labels"] = conn.execute(
        "select x as label, count(*) n from message_location l, unnest(l.labels) x where l.present"
        " group by 1 order by 2 desc limit 30").fetchall()
    return one


# The archive overview (Insights, Sources) counts the whole archive: kept in insight_cache, shown at once and
# counted again behind the page when new mail, a sync or new events have come since.
OVERVIEW = "overview"


def overview_fingerprint(conn: psycopg.Connection) -> str:
    r = conn.execute("select (select coalesce(max(id), 0) from message) as m, (select coalesce(max(id), 0) from event) as e,"
                     " (select coalesce(max(id), 0) from sync_run) as s").fetchone()
    return insight.fingerprint([r, insight.table_writes(conn, ["message", "message_location", "account", "attachment", "event"])])


def overview_cached(conn: psycopg.Connection, *, dsn: str | None = None, refresh: bool = False) -> dict:
    row = insight.get(conn, OVERVIEW, overview_fingerprint,
                      lambda c: json.loads(json.dumps(overview(c), default=insight.iso)), dsn=dsn, refresh=refresh)
    return {**row["payload"], "computed_at": row["computed_at"], "stale": row["stale"], "refreshing": row["refreshing"]}
