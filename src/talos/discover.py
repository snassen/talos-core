"""Discover: patterns in the mail that nobody asked about, and saved aggregations (docs/workspace.md).

The insights are deterministic queries over the archive and its metadata, computed together and
kept in insight_cache (refreshed behind the page when new mail or a new day makes them stale):

- **waiting**: conversations where a person asked the owner something and they have not answered
  (talos.importance's waiting, the last 60 days).
- **renewals**: renewals, licences and expiries that came from the same sender in the same month
  in two years or more, for the months ahead: what is likely to land again.
- **no-binder**: organisations the owner writes to (three threads or more in a year) that no binder
  mentions in its name, terms, description or text.
- **quiet**: automated senders that wrote regularly and then stopped (marketing left out):
  a backup, a report or a system that may have died without saying so.
- **new-people**: people who first wrote in the last 45 days, whom the owner has answered.

An aggregation is a Messages selection (the view's query string) counted in groups; it is counted
when shown. Claude's first ones come with migration 025; the owner can save their own from Messages.

Everything here reads the archive; only aggregations are written, to Talos's own table.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import urlencode

import psycopg

from talos import importance, insight, personal, search

NAME = "discover"
CONSUMER = ("gmail.com", "googlemail.com", "hotmail.com", "hotmail.se", "outlook.com", "live.com", "live.se",
            "icloud.com", "me.com", "mac.com", "yahoo.com", "msn.com", "protonmail.com", "proton.me")
GROUPS = {"sender": "m.from_address", "domain": "split_part(m.from_address, '@', 2)",
          "year": "extract(year from m.received_at)::int::text", "month": "to_char(m.received_at, 'YYYY-MM')",
          "account": "m.account_id"}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


class DiscoverError(ValueError):
    pass


def _mine(conn: psycopg.Connection) -> list[str]:
    """The owner's own domains (not the consumer ones): mail from them is not an outside organisation."""
    return [r["d"] for r in conn.execute("select distinct split_part(address, '@', 2) as d from my_address")
            if r["d"] not in CONSUMER]


def _binder_text(conn: psycopg.Connection) -> str:
    return " ".join(r["t"] for r in conn.execute(
        "select lower(name || ' ' || coalesce(description, '') || ' ' || body || ' ' || coalesce(attrs->>'terms', ''))"
        " as t from object where not archived"))


def _root(domain: str) -> str:
    """The name part of a domain: arrow.com → arrow, mail.example.se → example."""
    parts = domain.split(".")
    return parts[-2] if len(parts) >= 2 else domain


def _q(**kw) -> str:
    return urlencode([(k, v) for k, v in kw.items() if v not in (None, "")])


def waiting(conn: psycopg.Connection) -> dict:
    rows = importance.waiting(conn, limit=50, days=60)
    items = [{"label": r["from_name"] or r["from_address"], "detail": r["subject"], "message_id": r["id"],
              "days": float(r["waited_days"])} for r in rows]
    return {"key": "waiting", "tag": "Forgotten", "count": len(items),
            "title": "People waiting for your answer" if items else "Nobody is waiting for an answer",
            "text": (f"{len(items)} {'conversation' if len(items) == 1 else 'conversations'} where someone asked you"
                     " something in the last 60 days and you have not answered, oldest first.") if items else
                    "Every question from a person in the last 60 days has an answer.",
            "items": items[:25], "more": _q(important="1", direction="in") if items else None}


def renewals(conn: psycopg.Connection, now: datetime) -> dict:
    mine = _mine(conn)
    rows = conn.execute(
        "with r as (select split_part(m.from_address, '@', 2) as dom, extract(month from m.received_at)::int as mon,"
        " extract(year from m.received_at)::int as yr, m.subject from message m"
        " where m.direction = 'in' and m.received_at > now() - interval '6 years'"
        " and m.subject ~* '(renew|förny|expir|utgår|förfaller|går ut|up for renewal|licen[sc]|avtalet)')"
        " , spread as (select dom, count(distinct mon) as months from r group by dom)"
        " select r.dom, r.mon, count(distinct r.yr) as years, count(*) as n, max(r.yr) as last_year,"
        " (array_agg(r.subject order by r.yr desc))[1] as example from r join spread s on s.dom = r.dom"
        " where s.months <= 6 and r.dom <> all(%s)"  # a sender every month is a report, not a renewal
        " group by r.dom, r.mon having count(distinct r.yr) >= 2", (mine,)).fetchall()
    ahead = []
    for r in rows:
        until = (r["mon"] - now.month) % 12
        if until <= 3:
            ahead.append({**r, "until": until})
    ahead.sort(key=lambda r: (r["until"], -r["years"], -r["n"]))
    items = [{"label": r["dom"], "detail": r["example"], "n": r["n"],
              "note": f"{MONTHS[r['mon'] - 1]}, {r['years']} years" + (" · this month" if r["until"] == 0 else ""),
              "query": _q(domain=r["dom"], q="renew OR förnya OR expire OR utgår OR licens")} for r in ahead]
    months = sorted({MONTHS[r["mon"] - 1] for r in ahead}, key=MONTHS.index)
    return {"key": "renewals", "tag": "Recurring", "count": len(items),
            "title": (f"{len(items)} renewals come back in the months ahead" if items else "No yearly renewals ahead"),
            "text": ("Renewals, licences and expiries that arrived from the same sender in the same month in two"
                     " years or more, for " + ", ".join(months) + ".") if items else
                    "Nothing has renewed in the same month two years running for the next three months.",
            "items": items[:25]}


def no_binder(conn: psycopg.Connection) -> dict:
    skip = list(CONSUMER) + _mine(conn)
    rows = conn.execute(
        "select split_part(p.address, '@', 2) as dom, count(distinct m.thread_id) as threads,"
        " count(distinct p.address) as people, max(m.received_at) as last"
        " from message m join participant p on p.message_id = m.id and p.role in ('to', 'cc')"
        " where m.direction = 'out' and m.received_at > now() - interval '365 days'"
        " and split_part(p.address, '@', 2) <> all(%s) and p.address like '%%@%%.%%'"
        " group by 1 having count(distinct m.thread_id) >= 3 order by 2 desc limit 60", (skip,)).fetchall()
    text = _binder_text(conn)
    items = []
    for r in rows:
        root = _root(r["dom"])
        if len(root) >= 3 and (root in text or r["dom"] in text):
            continue
        items.append({"label": r["dom"], "n": r["threads"], "name": root.capitalize(),
                      "note": f"{r['threads']} threads you wrote in, {r['people']} {'person' if r['people'] == 1 else 'people'}",
                      "query": _q(domain=r["dom"])})
    return {"key": "no-binder", "tag": "Cross-connection", "count": len(items),
            "title": (f"{len(items)} organisations you write to have no binder" if items else
                      "Every organisation you write to has a binder"),
            "text": "You wrote to them in three threads or more this year, and no binder mentions them.",
            "items": items[:25]}


def quiet(conn: psycopg.Connection) -> dict:
    cands = conn.execute(
        "with s as (select m.from_address as a, max(m.received_at) as last, count(*) as n, min(m.account_id) as acct"
        " from message m where m.is_automated and m.direction = 'in' and m.from_address is not null"
        " and m.received_at > now() - interval '545 days' group by 1"
        " having max(m.received_at) between now() - interval '180 days' and now() - interval '10 days'"
        " and count(*) >= 12),"
        " y as (select s.*, (select count(*) from message x where x.from_address = s.a"
        " and x.received_at between s.last - interval '365 days' and s.last) as ny from s)"
        " select * from y where ny >= 12 and last < now() - make_interval(days => greatest(10, (4 * 365.0 / ny)::int))"
        " order by ny desc limit 60").fetchall()
    items = []
    for c in cands:
        # marketing that stopped is an unsubscribe, not a system gone quiet
        k = conn.execute(
            "select count(*) filter (where e.value in ('offer', 'announcement', 'spam')) as mk, count(*) as n"
            " from (select id from message where from_address = %s order by received_at desc limit 40) x"
            " join effective_message_assignment e on e.message_id = x.id and e.dimension_id = 'kind'",
            (c["a"],)).fetchone()
        if k["n"] and k["mk"] >= 0.4 * k["n"]:
            continue
        per = 365.0 / c["ny"]
        every = "every day" if per < 1.5 else f"every {round(per)} days" if per < 25 else "about monthly"
        items.append({"label": c["a"], "account_id": c["acct"], "n": c["ny"], "last": c["last"],
                      "note": f"{c['ny']} in the year before, {every}; last {c['last']:%Y-%m-%d}",
                      "query": _q(sender=c["a"])})
        if len(items) >= 10:
            break
    return {"key": "quiet", "tag": "Gone quiet", "count": len(items),
            "title": (f"{len(items)} regular senders went quiet" if items else "No regular sender has gone quiet"),
            "text": "Automated senders that wrote regularly and then stopped: a backup, a report or a system may"
                    " have died without saying so. Marketing that stopped is left out.",
            "items": items}


def new_people(conn: psycopg.Connection) -> dict:
    rows = conn.execute(
        "with f as (select from_address as a, min(received_at) as first, count(*) as n,"
        " (array_agg(from_name order by received_at desc))[1] as name from message"
        " where direction = 'in' and not is_automated and from_address is not null group by 1"
        " having min(received_at) > now() - interval '45 days' and count(*) >= 2)"
        " select f.* from f where exists (select 1 from message m join participant p on p.message_id = m.id"
        " where m.direction = 'out' and p.address = f.a) order by f.n desc limit 10").fetchall()
    items = [{"label": r["name"] or r["a"], "detail": r["a"], "n": r["n"],
              "note": f"first wrote {r['first']:%Y-%m-%d}", "query": _q(sender=r["a"])} for r in rows]
    return {"key": "new-people", "tag": "New", "count": len(items),
            "title": (f"{len(items)} new people you have answered" if items else "No new people lately"),
            "text": "People who first wrote in the last 45 days, and whom you have written back to.",
            "items": items}


def compute(conn: psycopg.Connection) -> dict:
    now = datetime.now(timezone.utc)
    return {"at": now, "insights": [waiting(conn), renewals(conn, now), no_binder(conn), quiet(conn), new_people(conn)]}


def fingerprint(conn: psycopg.Connection) -> str:
    r = conn.execute("select (select max(id) from message) m,"
                     " (select md5(string_agg(id || name || coalesce(attrs->>'terms', '') || archived, ',' order by id))"
                     " from object) o").fetchone()
    return insight.fingerprint([r, datetime.now(timezone.utc).date()])


def state(conn: psycopg.Connection, *, dsn: str | None = None, refresh: bool = False) -> dict:
    row = insight.get(conn, NAME, fingerprint, compute, dsn=dsn, refresh=refresh, first_behind=bool(dsn))
    return {**(row["payload"] or {"insights": []}), "computed_at": row["computed_at"], "seconds": row["seconds"],
            "stale": row["stale"], "refreshing": row["refreshing"]}


# ---------------------------------------------------------------- aggregations

def aggregations(conn: psycopg.Connection) -> list[dict]:
    return conn.execute("select * from aggregation order by made_by desc, created_at, id").fetchall()


def run(conn: psycopg.Connection, agg: dict, *, top: int = 12) -> dict:
    """The aggregation counted now: its total and its largest groups, each with its own selection."""
    key = GROUPS[agg["group_by"]]
    sql, p = search.messages_sql(**search.filters_from_query(agg["query"]), select=f"{key} as g")
    rows = conn.execute(f"select g, count(*) as n from ({sql}) x where g is not null and g <> ''"
                        f" group by g order by {'g desc' if agg['group_by'] in ('year', 'month') else 'n desc, g'}", p).fetchall()
    total = sum(r["n"] for r in rows)
    extra = {"sender": "sender", "domain": "domain", "account": "account"}.get(agg["group_by"])
    groups = [{"key": r["g"], "n": r["n"],
               "query": agg["query"] + (("&" if agg["query"] else "") + urlencode([(extra, r["g"])]) if extra else "")}
              for r in rows[:top]]
    return {**agg, "total": total, "count": len(rows), "groups": groups}


def add(conn: psycopg.Connection, name: str, query: str, group_by: str, *, description: str = "",
        made_by: str = personal.OWNER_ID) -> int:
    name = (name or "").strip()
    if not name:
        raise DiscoverError("an aggregation needs a name")
    if group_by not in GROUPS:
        raise DiscoverError("group by sender, domain, year, month or account")
    query = (query or "").strip().lstrip("?")
    if not any(search.filters_from_query(query).values()):
        raise DiscoverError("an aggregation needs a search or a filter to count")
    return conn.execute("insert into aggregation (name, description, query, group_by, made_by)"
                        " values (%s, %s, %s, %s, %s) returning id",
                        (name[:200], (description or "").strip()[:500], query, group_by, made_by)).fetchone()["id"]


def remove(conn: psycopg.Connection, agg_id: int) -> None:
    if not conn.execute("delete from aggregation where id = %s returning id", (agg_id,)).fetchone():
        raise DiscoverError("no such aggregation")


# The saved aggregations, counted: kept in insight_cache. Adding or removing one shows at once (their rows are
# the exact part of the fingerprint); new mail and new values are counted again behind the page.
AGGREGATIONS = "aggregations"


def _saved_fingerprint(conn: psycopg.Connection) -> str:
    return insight.fingerprint(conn.execute(
        "select count(*) as n, coalesce(string_agg(id || ':' || md5(coalesce(name, '') || coalesce(query, '') || coalesce(group_by, '')), ','"
        " order by id), '') as k from aggregation").fetchone())


def _counted_fingerprint(conn: psycopg.Connection) -> str:
    r = conn.execute("select coalesce(max(id), 0) as m from message").fetchone()
    return insight.fingerprint([r, insight.table_writes(conn, ["message", "assignment", "edge"])])


def aggregations_counted(conn: psycopg.Connection, *, dsn: str | None = None, refresh: bool = False) -> dict:
    row = insight.get_own(conn, AGGREGATIONS, _saved_fingerprint, _counted_fingerprint,
                          lambda c: {"rows": json.loads(json.dumps([run(c, a) for a in aggregations(c)], default=insight.iso))},
                          dsn=dsn, refresh=refresh)
    return {**row["payload"], "computed_at": row["computed_at"], "stale": row["stale"], "refreshing": row["refreshing"]}
