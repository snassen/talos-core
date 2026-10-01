"""What the matching messages have in common: senders, types, topics and labels.

The Messages view asks these next to its list, with the same filters, so the sender
sidebar and the filter dropdowns always describe the current selection. Each answer is
one grouped query over the ids that search.messages_sql matches (its `select` form), so
the filters mean exactly what they mean for the list.

A dimension's values carry their label and family from the taxonomy (dimension.value_meta), and
their place in its list (`order`), so the dropdowns can show words grouped by family; a value in
use that the taxonomy does not describe has neither, and shows as its id.

A facet ignores its own filter: with type=newsletter chosen, the type counts still list
the other types, so the dropdown can switch between them. The UI does the same for the
sender sidebar by leaving `sender` out when it asks.
"""

from __future__ import annotations

import psycopg

from talos import search

MAX_SENDERS = 1000
MAX_VALUES = 300
FACETS = ("kind", "type", "topic", "origin", "sphere", "keep", "label", "account")
ACCOUNT_FILTERS = ("account", "accounts", "exclude_accounts")


def _machine(filters: dict) -> str:
    """Whether each message was made by a machine, as People/Automated decides it (by sender_kind,
    then origin, else the automated flag). Under that filter every message agrees with it, so
    nothing is computed."""
    automated = filters.get("automated")
    return search.MACHINE_COLUMN if automated is None else ("true" if automated else "false")


def senders_sql(q: str | None = None, *, limit: int = 20, **filters) -> tuple[str, dict]:
    """The senders of the matching messages, most messages first. A sender counts as automated
    when every one of those messages was made by a machine."""
    inner, params = search.messages_sql(
        q, select=f"m.from_address, m.from_name, ({_machine(filters)}) as is_automated",
        with_machine=filters.get("automated") is None, **filters)
    params["senders_limit"] = limit
    return (f"with f as ({inner}),"
            " s as (select from_address as address, count(*) as n, bool_and(is_automated) as automated,"
            "       any_value(nullif(from_name, '')) as from_name"
            "       from f where from_address is not null group by from_address)"
            " select address, n, automated, from_name, count(*) over () as senders, sum(n) over () as total"
            " from s order by n desc, address limit %(senders_limit)s", params)


def senders(conn: psycopg.Connection, q: str | None = None, *, limit: int = 20, **filters) -> dict:
    """Senders with counts and share. limit=0 means all of them, up to MAX_SENDERS."""
    limit = MAX_SENDERS if not limit or limit < 0 else min(limit, MAX_SENDERS)
    sql, params = senders_sql(q, limit=limit, **filters)
    search._work_mem(conn)
    rows = conn.execute(sql, params).fetchall()
    total = int(rows[0]["total"]) if rows else 0
    count = rows[0]["senders"] if rows else 0
    names = _names(conn, rows)
    return {"total": total, "senders": count, "limit": limit,
            "rows": [{"address": r["address"], "name": names.get(r["address"]), "n": r["n"],
                      "share": round(r["n"] / total, 4) if total else 0, "automated": r["automated"]}
                     for r in rows]}


def domains_sql(q: str | None = None, *, limit: int = 20, **filters) -> tuple[str, dict]:
    """The sender domains of the matching messages, most messages first."""
    inner, params = search.messages_sql(q, select=f"m.from_address, ({_machine(filters)}) as is_automated",
                                        with_machine=filters.get("automated") is None, **filters)
    params["domains_limit"] = limit
    return (f"with f as ({inner}),"
            " d as (select split_part(from_address, '@', 2) as domain, count(*) as n,"
            "       bool_and(is_automated) as automated, count(distinct from_address) as addresses"
            "       from f where from_address is not null group by 1)"
            " select domain, n, automated, addresses, count(*) over () as domains, sum(n) over () as total"
            " from d order by n desc, domain limit %(domains_limit)s", params)


def domains(conn: psycopg.Connection, q: str | None = None, *, limit: int = 20, **filters) -> dict:
    """Sender domains with counts and share, like senders(). limit=0 means up to MAX_SENDERS."""
    limit = MAX_SENDERS if not limit or limit < 0 else min(limit, MAX_SENDERS)
    sql, params = domains_sql(q, limit=limit, **filters)
    search._work_mem(conn)
    rows = conn.execute(sql, params).fetchall()
    total = int(rows[0]["total"]) if rows else 0
    return {"total": total, "domains": rows[0]["domains"] if rows else 0, "limit": limit,
            "rows": [{"domain": r["domain"], "n": r["n"], "addresses": r["addresses"],
                      "share": round(r["n"] / total, 4) if total else 0, "automated": r["automated"]}
                     for r in rows]}


def account_facet(conn: psycopg.Connection, q: str | None = None, **filters) -> list[dict]:
    """Every account the archive knows, and how many matching messages each holds.

    Like any facet it ignores its own filter (the account chips), so an account switched off
    still says how many it would add. An account is listed when it is enabled or has matches."""
    for k in ACCOUNT_FILTERS:
        filters.pop(k, None)
    inner, params = search.messages_sql(q, select="m.account_id", **filters)
    return conn.execute(
        f"with f as ({inner}), c as (select account_id, count(*) as n from f group by 1)"
        " select a.id as value, coalesce(nullif(a.display_name, ''), a.id) as name, a.provider,"
        " a.enabled, coalesce(c.n, 0) as n"
        " from account a left join c on c.account_id = a.id where a.enabled or c.n is not null"
        " order by a.id", params).fetchall()


def _names(conn: psycopg.Connection, rows: list[dict]) -> dict[str, str]:
    """The best display name per address: the person's name, else a name the mail itself gave.

    Ingest names a person after the first From name it sees; rows carry one of its From names as the
    fallback for an address without a person (a Teams user, say)."""
    addresses = [r["address"] for r in rows]
    names = {r["address"]: r["from_name"] for r in rows if r.get("from_name")}
    if addresses:
        names.update({r["address"]: r["name"] for r in conn.execute(
            "select a.address, p.display_name as name from address a join person p on p.id = a.person_id"
            " where a.address = any(%s) and coalesce(p.display_name, '') <> ''", (addresses,))})
    return names


def facet_sql(facet: str, q: str | None = None, *, many: bool = False, **filters) -> tuple[str, dict]:
    """Values and counts of one facet (a dimension id, or 'label') over the matching messages.

    many: the dimension holds many values per message, so every active value counts."""
    if facet == "label":
        inner, params = search.messages_sql(q, select="m.id", **filters)
        params["facet_limit"] = MAX_VALUES
        return (f"with f as ({inner})"
                " select x as value, count(distinct l.message_id) as n"
                " from f join message_location l on l.message_id = f.id and l.present and l.labels <> '{}',"
                " unnest(l.labels) x"
                " group by x order by n desc, x limit %(facet_limit)s", params)
    # effective_message_assignment answers one message at a time; asked for a whole selection
    # it ranks every assignment in the archive first (7 s on 255k messages). So the same
    # ranking is done here over the selection only: the messages' own active values and
    # their threads' compete, human > rule > model > import, then own before thread, then
    # the newest. A test holds the counts equal to the view's.
    inner, params = search.messages_sql(q, select="m.id, m.thread_id", **filters)
    params["facet_dim"], params["facet_limit"] = facet, MAX_VALUES
    candidates = (f"with f as materialized ({inner}),"
                  " c as (select f.id as message_id, a.id, a.value, a.source_kind, a.created_at, true as own"
                  "       from f join assignment a on a.entity_id = f.id"
                  "       where a.status = 'active' and a.dimension_id = %(facet_dim)s"
                  "       union all"
                  "       select f.id, a.id, a.value, a.source_kind, a.created_at, false"
                  "       from f join assignment a on a.entity_id = f.thread_id"
                  "       where a.status = 'active' and a.dimension_id = %(facet_dim)s)")
    if many:
        return (candidates + " select value, count(distinct message_id) as n from c"
                " group by value order by n desc, value limit %(facet_limit)s", params)
    return (candidates + ", winner as (select distinct on (message_id) message_id, value from c order by message_id,"
            " case source_kind when 'human' then 4 when 'rule' then 3 when 'model' then 2 else 1 end desc,"
            " own desc, created_at desc, id desc)"
            " select value, count(*) as n from winner group by value order by n desc, value limit %(facet_limit)s",
            params)


def facet(conn: psycopg.Connection, name: str, q: str | None = None, **filters) -> list[dict]:
    """One facet's values and counts, leaving out its own filter.

    name is a dimension id, 'label' or 'account'. Its own filter is the pairs for that
    dimension, the label, or the account filters."""
    if name == "account":
        return account_facet(conn, q, **filters)
    dim = None if name == "label" else conn.execute(
        "select cardinality, allowed, value_meta from dimension where id = %s", (name,)).fetchone()
    if name != "label" and not dim:
        raise ValueError(f"unknown facet {name!r}")
    dims = filters.pop("dimension", None)
    dims = [dims] if isinstance(dims, tuple) else list(dims or [])
    filters["dimension"] = [d for d in dims if d[0] != name]
    if name == "label":
        filters["label"] = None
    many = bool(dim) and dim["cardinality"] == "many"
    conn.execute("set local work_mem = '64MB'")  # the ranking sorts 100k+ rows; keep it in memory
    sql, params = facet_sql(name, q, many=many, **filters)
    rows = conn.execute(sql, params).fetchall()
    return with_labels(rows, dim) if dim else rows


def with_labels(rows: list[dict], dim: dict) -> list[dict]:
    """Each value's label, family and place in the dimension's list, where the taxonomy has them."""
    meta = dim.get("value_meta") or {}
    order = {v: i for i, v in enumerate(dim.get("allowed") or [])}
    for r in rows:
        m = meta.get(r["value"])
        if m:
            r.update(label=m.get("label") or r["value"], family=m.get("family") or "", order=order.get(r["value"]))
    return rows


def top_senders(conn: psycopg.Connection, *, days: int | None = None, people_only: bool = True,
                limit: int = 15) -> list[dict]:
    """The individual addresses that send the owner the most, never one of their own.

    people_only leaves out automated mail, so a person is ranked by what they wrote, and a
    no-reply address drops out. days limits it to recent mail; None means all of it."""
    where = ["m.direction = 'in'", "m.from_address is not null",
             "not exists (select 1 from my_address me where me.address = m.from_address)"]
    params: dict = {"limit": max(1, min(int(limit or 15), 200))}
    if people_only:
        where.append("not m.is_automated")
    if days:
        where.append("m.received_at >= now() - make_interval(days => %(days)s)")
        params["days"] = int(days)
    rows = conn.execute(
        "select m.from_address as address, count(*) as n, max(m.received_at) as latest,"
        " any_value(nullif(m.from_name, '')) as from_name from message m"
        f" where {' and '.join(where)} group by m.from_address order by n desc, address limit %(limit)s",
        params).fetchall()
    names = _names(conn, rows)
    for r in rows:
        r["name"] = names.get(r["address"])
        del r["from_name"]
        r["domain"] = r["address"].split("@", 1)[-1]
    return rows
