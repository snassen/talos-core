"""Deterministic rules: conditions as data, compiled to one SQL query each.

A rule is an ordered list of conditions, ANDed (an {"any": [...]} condition is an OR group), and
one action:

    {"id": "receipts-klarna", "priority": 50,
     "conditions": [{"field": "from_domain", "op": "is", "value": "klarna.com"},
                    {"field": "subject", "op": "contains", "value": "kvitto"}],
     "action": {"dimension": "type", "value": "receipt"}}

Because each rule becomes one INSERT … SELECT, re-running every rule over the whole
archive takes seconds, and preview() answers "how many, and which" before a rule
is saved, as Overcast's attribution editor does.

Ordering: rules run by priority (lower first). For a one-value dimension the first
matching rule wins; a later rule does not overwrite it. Human assignments are never
touched: a rule run deletes and rewrites only assignments whose source is a rule from
the rule table. Every assignment records "rule:<id>@<version>", so any value can be
traced back. Other rule-made values (the enrichment pre-pass, "prepass:…", talos.enrich)
are neither deleted by a rule run nor counted by its first-wins guard.

Removing a rule (remove) deletes it and the values and memberships it made, so each message
falls back to its next source at once (another rule at the next run, an accepted model value,
an import). The definition is kept in rule_removed, with when and why. A rule file (the owner's in
TALOS_HOME/config/rules, rules/example-rules.json in the repository) is saved with load_file:
idempotent, a version bump only where conditions or action changed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from talos import personal

# field -> SQL expression over message m. Values are always passed as parameters.
FIELDS: dict[str, str] = {
    "account": "m.account_id",
    "direction": "m.direction",
    "from_address": "m.from_address",
    "from_domain": "split_part(m.from_address, '@', 2)",
    "from_name": "coalesce(m.from_name, '')",
    "subject": "coalesce(m.subject, '')",
    "list_id": "coalesce(m.list_id, '')",
    "is_automated": "m.is_automated",
    "has_attachments": "m.has_attachments",
    "size": "m.size_bytes",
    "received": "m.received_at",
    "medium": "m.medium",
}
# Fields that live in other tables: tested with EXISTS.
EXISTS_FIELDS: dict[str, tuple[str, str]] = {
    # The readable path ("Inkorgen/Backup"); for Graph, l.folder is the folder id.
    "folder": ("message_location l", "l.message_id = m.id and l.present and {cmp}", "coalesce(l.folder_path, l.folder)"),
    "label": ("message_location l", "l.message_id = m.id and l.present and {cmp}", "lbl"),
    "recipient": ("participant p", "p.message_id = m.id and p.role in ('to', 'cc', 'bcc') and {cmp}", "p.address"),
    "attachment_type": ("attachment a", "a.message_id = m.id and {cmp}", "a.content_type"),
    "attachment_name": ("attachment a", "a.message_id = m.id and {cmp}", "coalesce(a.filename, '')"),
    # The parser's reasons for is_automated: auto-submitted, precedence, list, noreply-sender,
    # null-return-path (message.headers -> 'automated').
    "automated_reason": ("jsonb_array_elements_text(coalesce(m.headers -> 'automated', '[]'::jsonb)) ar(reason)",
                         "{cmp}", "ar.reason"),
}
# The source_ref of every assignment made by the rule table ("rule:<id>@<version>").
RULE_REF = "rule:%"
TEXT_OPS = {"is", "is_not", "contains", "not_contains", "starts_with", "ends_with", "matches", "in"}
OPS = TEXT_OPS | {"is_set", "is_true", "is_false", "before", "after", "gt", "lt", "search"}


class RuleError(ValueError):
    pass


@dataclass
class Rule:
    id: str
    name: str
    conditions: list[dict]
    action: dict
    priority: int = 100
    version: int = 1
    enabled: bool = True
    description: str | None = None
    origin: dict | None = None   # where Talos made it from: a suggestion, or a ticked group of them

    @property
    def ref(self) -> str:
        return f"rule:{self.id}@{self.version}"


def _cmp(expr: str, op: str, value, params: list) -> str:
    if op == "is":
        params.append(str(value).lower())
        return f"lower({expr}) = %s"
    if op == "is_not":
        params.append(str(value).lower())
        return f"lower({expr}) <> %s"
    if op in ("contains", "not_contains"):
        params.append(f"%{_like(str(value))}%")
        return f"{expr} {'not ' if op == 'not_contains' else ''}ilike %s"
    if op == "starts_with":
        params.append(f"{_like(str(value))}%")
        return f"{expr} ilike %s"
    if op == "ends_with":
        params.append(f"%{_like(str(value))}")
        return f"{expr} ilike %s"
    if op == "matches":
        params.append(str(value))
        return f"{expr} ~* %s"
    if op == "in":
        if not isinstance(value, list):
            raise RuleError("'in' needs a list")
        params.append([str(v).lower() for v in value])
        return f"lower({expr}) = any(%s)"
    raise RuleError(f"operator {op!r} does not apply to text")


def _like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def compile_conditions(conditions: list[dict]) -> tuple[str, list]:
    """Return (where-clause over message m, params). An empty list matches nothing.

    The list is ANDed. A condition {"any": [alternative, …]} is an OR group: each alternative is
    one condition or a list of conditions (ANDed), and may hold an "any" of its own. So a rule
    made from a whole category of suggestions reads

        [{"any": [{"field": "from_address", "op": "in", "value": [a, b, c]},
                  [{"field": "from_address", "op": "is", "value": d},
                   {"field": "subject", "op": "matches", "value": "^backup report"}]]}]"""
    if not conditions:
        return "false", []
    params: list = []
    return _all(conditions, params), params


def _all(conditions, params: list) -> str:
    if not isinstance(conditions, list) or not conditions:
        raise RuleError("a group of conditions needs at least one condition")
    return " and ".join(f"({_one(c, params)})" for c in conditions)


def _one(c, params: list) -> str:
    if not isinstance(c, dict):
        raise RuleError("a condition is a {field, op, value} object or an {any: [...]} group")
    if "any" in c:
        alts = c["any"]
        if not isinstance(alts, list) or not alts:
            raise RuleError("'any' needs a list of alternatives")
        return " or ".join(f"({_all(a if isinstance(a, list) else [a], params)})" for a in alts)
    field, op, value = c.get("field"), c.get("op"), c.get("value")
    if op not in OPS:
        raise RuleError(f"unknown operator {op!r}")
    if field == "body":
        if op != "search":
            raise RuleError("body supports only 'search'")
        params.extend([value, value])
        return ("exists (select 1 from message_text t where t.message_id = m.id and t.search @@"
                " (websearch_to_tsquery('swedish', %s) || websearch_to_tsquery('english', %s)))")
    if field in EXISTS_FIELDS:
        table, where, expr = EXISTS_FIELDS[field]
        if field == "label":
            table = "message_location l, unnest(l.labels) lbl"
        cmp = "true" if op == "is_set" else _cmp(expr, op, value, params)
        return f"exists (select 1 from {table} where {where.format(cmp=cmp)})"
    if field in FIELDS:
        expr = FIELDS[field]
        if op == "is_true":
            return f"{expr} is true"
        if op == "is_false":
            return f"{expr} is not true"
        if op == "is_set":
            return f"nullif({expr}::text, '') is not null"
        if op in ("before", "after", "gt", "lt"):
            params.append(value)
            return f"{expr} {'<' if op in ('before', 'lt') else '>'} %s"
        return _cmp(expr, op, value, params)
    if field and field.startswith("header:"):
        key = field.split(":", 1)[1]
        params.append(key)
        expr = "coalesce(m.headers ->> %s, '')"
        if op == "is_set":
            return f"nullif({expr}, '') is not null"
        return _cmp(expr, op, value, params)
    raise RuleError(f"unknown field {field!r}")


def leaves(conditions) -> list[dict]:
    """Every plain {field, op, value} condition, however deep in "any" groups."""
    out: list[dict] = []
    for c in conditions if isinstance(conditions, list) else [conditions]:
        if isinstance(c, list):
            out.extend(leaves(c))
        elif isinstance(c, dict) and "any" in c:
            out.extend(leaves(c["any"]))
        elif isinstance(c, dict):
            out.append(c)
    return out


def check(conn: psycopg.Connection, conditions: list[dict]) -> tuple[str, list]:
    """Compile the conditions and make sure PostgreSQL accepts them, or raise RuleError.

    compile_conditions only checks the shape. A regular expression or a date is checked by
    the database, and only when a row is compared, so an empty archive would accept a broken
    one. Each 'matches' pattern is therefore tried on its own, and the query is run once
    inside a savepoint, so a mistake is a RuleError (a 400 in the UI), not a failed rule run."""
    if not isinstance(conditions, list) or not all(isinstance(c, dict) for c in conditions):
        raise RuleError("conditions must be a list of {field, op, value} objects")
    where, params = compile_conditions(conditions)
    try:
        with conn.transaction():
            for c in leaves(conditions):
                if c.get("op") == "matches":
                    conn.execute("select '' ~* %s", (str(c.get("value")),))
            conn.execute(f"select 1 from message m where {where} limit 1", params)
    except psycopg.Error as exc:
        raise RuleError(f"PostgreSQL does not accept these conditions: {exc.diag.message_primary or exc}") from exc
    return where, params


def preview(conn: psycopg.Connection, conditions: list[dict], *, sample: int = 10) -> dict:
    where, params = check(conn, conditions)
    n = conn.execute(f"select count(*) as n from message m where {where}", params).fetchone()["n"]
    rows = conn.execute(
        f"select m.id, m.received_at, m.from_address, m.subject from message m where {where}"
        f" order by m.received_at desc nulls last limit %s", [*params, sample]).fetchall()
    return {"count": n, "sample": rows}


def save(conn: psycopg.Connection, rule: Rule) -> Rule:
    """Insert or update a rule. A change to conditions or action bumps the version."""
    check(conn, rule.conditions)  # validate before storing
    _validate_action(conn, rule.action)
    row = conn.execute("select version, conditions, action from rule where id = %s", (rule.id,)).fetchone()
    if row:
        changed = row["conditions"] != rule.conditions or row["action"] != rule.action
        rule.version = row["version"] + (1 if changed else 0)
        conn.execute(
            "update rule set version = %s, name = %s, description = %s, enabled = %s, priority = %s,"
            " conditions = %s, action = %s, origin = coalesce(%s, origin), updated_at = now() where id = %s",
            (rule.version, rule.name, rule.description, rule.enabled, rule.priority,
             Jsonb(rule.conditions), Jsonb(rule.action), Jsonb(rule.origin) if rule.origin else None, rule.id))
    else:
        conn.execute(
            "insert into rule (id, version, name, description, enabled, priority, conditions, action, origin)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (rule.id, rule.version, rule.name, rule.description, rule.enabled, rule.priority,
             Jsonb(rule.conditions), Jsonb(rule.action), Jsonb(rule.origin) if rule.origin else None))
    return rule


def _ref_prefix(rule_id: str) -> str:
    """The start of every source_ref this rule made, any version: 'rule:<id>@'. An id holds no '@'."""
    return f"rule:{rule_id}@"


def remove(conn: psycopg.Connection, rule_id: str, *, why: str | None = None, who: str = personal.OWNER_ID) -> dict:
    """Remove a rule: keep its definition in rule_removed, delete the values and memberships it made
    (every version), then the rule. Each message then falls back to its next source. Human values are
    never touched; nor are the pre-pass's. Raises RuleError for an unknown id."""
    with conn.transaction():
        row = conn.execute("select * from rule where id = %s for update", (rule_id,)).fetchone()
        if not row:
            raise RuleError(f"no rule {rule_id!r}")
        definition = {k: row[k] for k in ("id", "version", "name", "description", "enabled", "priority",
                                          "conditions", "action", "origin")}
        definition["created_at"] = row["created_at"].isoformat() if row.get("created_at") else None
        conn.execute("insert into rule_removed (rule_id, definition, why, removed_by) values (%s, %s, %s, %s)",
                     (rule_id, Jsonb(definition), (why or "").strip() or None, who))
        values = conn.execute(
            "delete from assignment where source_kind = 'rule' and starts_with(source_ref, %s)",
            (_ref_prefix(rule_id),)).rowcount
        edges = conn.execute("delete from edge where starts_with(source, %s)", (_ref_prefix(rule_id),)).rowcount
        conn.execute("delete from rule where id = %s", (rule_id,))
    return {"id": rule_id, "version": row["version"], "values": values, "edges": edges}


def remove_all(conn: psycopg.Connection, *, keep: list[str] | tuple = (), why: str | None = None,
               who: str = personal.OWNER_ID) -> list[dict]:
    """Remove every rule except those in keep (an unknown id in keep is refused, so a typo cannot
    remove the rule it meant to keep)."""
    ids = [r["id"] for r in conn.execute("select id from rule order by priority, id")]
    unknown = sorted(set(keep) - set(ids))
    if unknown:
        raise RuleError(f"--keep names rules that do not exist: {', '.join(unknown)}")
    with conn.transaction():
        return [remove(conn, rid, why=why, who=who) for rid in ids if rid not in set(keep)]


def removed(conn: psycopg.Connection, *, limit: int = 50) -> list[dict]:
    """The removal history, newest first."""
    return conn.execute("select id, rule_id, definition, removed_at, why, removed_by from rule_removed"
                        " order by removed_at desc, id desc limit %s", (limit,)).fetchall()


RULE_FIELDS = ("id", "name", "conditions", "action", "priority", "enabled", "description")


def read_file(path: Path | str) -> list[Rule]:
    """The rules in a file: a list of rules, one rule, or {"rules": [...], "note": ...}."""
    data = json.loads(Path(path).read_text())
    items = data["rules"] if isinstance(data, dict) and "rules" in data else data if isinstance(data, list) else [data]
    out, seen = [], set()
    for i, item in enumerate(items, 1):
        extra = set(item) - set(RULE_FIELDS)
        if extra:
            raise RuleError(f"rule {i} ({item.get('id')}): unknown keys {', '.join(sorted(extra))}")
        if not item.get("id") or not item.get("name"):
            raise RuleError(f"rule {i}: it needs an id and a name")
        if item["id"] in seen:
            raise RuleError(f"the id {item['id']!r} is in the file twice")
        seen.add(item["id"])
        out.append(Rule(**item))
    return out


def load_file(conn: psycopg.Connection, path: Path | str) -> list[dict]:
    """Save every rule of a file, all or none. Idempotent: a rule whose conditions and action are
    unchanged keeps its version (its name, description, priority and switch follow the file)."""
    rules = read_file(path)
    out = []
    with conn.transaction():
        for rule in rules:
            before = conn.execute("select version, name, description, enabled, priority, conditions, action"
                                  " from rule where id = %s", (rule.id,)).fetchone()
            save(conn, rule)
            if before is None:
                state = "created"
            elif before["version"] != rule.version:
                state = "new version"
            elif any(before[k] != getattr(rule, k) for k in ("name", "description", "enabled", "priority")):
                state = "updated"
            else:
                state = "unchanged"
            out.append({"id": rule.id, "version": rule.version, "state": state, "enabled": rule.enabled})
    return out


def _validate_action(conn: psycopg.Connection, action: dict) -> None:
    if "dimension" in action:
        d = conn.execute("select allowed from dimension where id = %s", (action["dimension"],)).fetchone()
        if not d:
            raise RuleError(f"unknown dimension {action['dimension']!r}")
        if d["allowed"] and action.get("value") not in d["allowed"]:
            raise RuleError(f"{action.get('value')!r} is not an allowed value of {action['dimension']}")
        if not action.get("value"):
            raise RuleError("a dimension action needs a value")
    elif "object" in action:
        if not conn.execute("select 1 from object where id = %s", (action["object"],)).fetchone():
            raise RuleError(f"unknown object {action['object']!r}")
    else:
        raise RuleError("action needs 'dimension' or 'object'")


def load(conn: psycopg.Connection, *, enabled_only: bool = True) -> list[Rule]:
    rows = conn.execute(
        "select * from rule" + (" where enabled" if enabled_only else "") + " order by priority, id").fetchall()
    return [Rule(id=r["id"], name=r["name"], conditions=r["conditions"], action=r["action"], priority=r["priority"],
                 version=r["version"], enabled=r["enabled"], description=r["description"], origin=r.get("origin"))
            for r in rows]


def run_all(conn: psycopg.Connection) -> dict[str, int]:
    """Recompute every assignment and membership made by the rule table. Human ones, and
    the pre-pass's rule-made ones (source_ref 'prepass:…'), are untouched."""
    counts: dict[str, int] = {}
    with conn.transaction():
        conn.execute("delete from assignment where source_kind = 'rule' and source_ref like %s", (RULE_REF,))
        conn.execute("delete from edge where source like 'rule:%%'")
        for rule in load(conn):
            counts[rule.id] = _apply(conn, rule)
    return counts


def _apply(conn: psycopg.Connection, rule: Rule) -> int:
    where, params = compile_conditions(rule.conditions)
    action = rule.action
    if "dimension" in action:
        dim = conn.execute("select cardinality from dimension where id = %s", (action["dimension"],)).fetchone()
        first_wins = dim["cardinality"] == "one"
        guard = ("and not exists (select 1 from assignment x where x.entity_id = m.id and x.dimension_id = %s"
                 " and x.source_kind = 'rule' and x.source_ref like %s and x.status = 'active')") if first_wins else \
                ("and not exists (select 1 from assignment x where x.entity_id = m.id and x.dimension_id = %s"
                 " and x.value = %s and x.source_kind = 'rule' and x.source_ref like %s and x.status = 'active')")
        guard_params = [action["dimension"], RULE_REF] if first_wins else [action["dimension"], action["value"], RULE_REF]
        cur = conn.execute(
            f"insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, confidence)"
            f" select m.id, %s, %s, 'rule', %s, 1.0 from message m where ({where}) {guard}",
            [action["dimension"], action["value"], rule.ref, *params, *guard_params])
        return cur.rowcount
    cur = conn.execute(
        f"insert into edge (src, rel, dst, source) select m.id, 'member_of', %s, %s from message m"
        f" where ({where}) on conflict do nothing",
        [action["object"], rule.ref, *params])
    return cur.rowcount


def assign(conn: psycopg.Connection, entity_ids: list[int], dimension: str, value: str, *,
           who: str = personal.OWNER_ID) -> int:
    """A human decision. It supersedes earlier human values in a one-value dimension."""
    d = conn.execute("select cardinality, allowed from dimension where id = %s", (dimension,)).fetchone()
    if not d:
        raise RuleError(f"unknown dimension {dimension!r}")
    if d["allowed"] and value not in d["allowed"]:
        raise RuleError(f"{value!r} is not an allowed value of {dimension}")
    if d["cardinality"] == "one":
        conn.execute(
            "update assignment set status = 'superseded' where entity_id = any(%s) and dimension_id = %s"
            " and source_kind = 'human' and status = 'active'", (entity_ids, dimension))
    cur = conn.execute(
        sql.SQL("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, decided_by,"
                " decided_at) select unnest(%s::bigint[]), %s, %s, 'human', %s, %s, now()"),
        (entity_ids, dimension, value, who, who))
    return cur.rowcount
