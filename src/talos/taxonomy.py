"""The closed value lists (rules/taxonomy.json) loaded into the dimension table.

`load(conn, path)` sets, for every dimension in the file, its label, description,
cardinality, `allowed` (the values in file order) and `value_meta` (value → family, label,
description). The file is the source of truth; the database follows it.

A dimension may be **derived** from another (`derived_from`: the source dimension, and either
`families`, for every family of the source's values the value it maps to, or `values`, for every
value of the dimension the source values under it). The four boundaries of two-level acceptance
map families (talos.boundary): sender_kind from origin, sphere from topic, form from type and keep
from value. `kind` maps values: each of its eleven values lists the types under it. Every family
(or every value) of the source must map to exactly one of the dimension's values, so a new family
or type cannot slip through unmapped, nor sit under two kinds; the mapping is stored in
`dimension.derived_from`. A key given twice anywhere in the file is refused (JSON would quietly
keep the last).

A load is refused as a whole, and nothing is written, when a value is still in use but no
longer in the file: an active assignment, a saved rule's action (enabled or not) or an answer
in the answer key. The refusal lists them. One exception: an answer-key route label that holds
an object id (digits only) is route's old meaning, not a value; the answer key reads it as
unset (gold_label_valid), so it does not block the load.

Idempotent: a second load of the same file writes nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from talos import personal

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "rules" / "taxonomy.json"


def current() -> Path:
    """The owner's taxonomy (TALOS_HOME/config/taxonomy.json, talos.personal), else the repository's."""
    return personal.find("taxonomy.json", DEFAULT_PATH)

CARDINALITIES = ("one", "many")
GOLD_FIELDS = ("origin", "type", "topic", "ask", "value", "route", "kind")


class TaxonomyError(ValueError):
    pass


def read(path: Path | str | None = None) -> dict[str, dict]:
    """The file's dimensions, checked: {id: {label, cardinality, description, values: [...]}}."""
    path = Path(path) if path else current()
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicates)
    except (OSError, json.JSONDecodeError) as exc:
        raise TaxonomyError(f"cannot read {path}: {exc}") from None
    dims = data.get("dimensions") if isinstance(data, dict) else None
    if not isinstance(dims, dict) or not dims:
        raise TaxonomyError(f"{path}: no dimensions")
    out = {}
    for dim, d in dims.items():
        if not isinstance(d, dict) or not isinstance(d.get("values"), list) or not d["values"]:
            raise TaxonomyError(f"{dim}: needs a non-empty list of values")
        if d.get("cardinality") not in CARDINALITIES:
            raise TaxonomyError(f"{dim}: cardinality is one of {', '.join(CARDINALITIES)}")
        seen = set()
        for v in d["values"]:
            if not isinstance(v, dict) or not all(isinstance(v.get(k), str) and v[k].strip()
                                                  for k in ("value", "family", "label", "description")):
                raise TaxonomyError(f"{dim}: every value needs value, family, label and description ({v!r})")
            if v["value"] in seen:
                raise TaxonomyError(f"{dim}: {v['value']!r} is listed twice")
            seen.add(v["value"])
        out[dim] = {"label": d.get("label") or dim, "cardinality": d["cardinality"],
                    "description": d.get("description"), "values": d["values"],
                    "derived_from": d.get("derived_from")}
    for dim, d in out.items():
        if d["derived_from"] is not None:
            _check_derived(dim, d, out)
    return out


def _no_duplicates(pairs: list) -> dict:
    keys = [k for k, _ in pairs]
    twice = sorted({k for k in keys if keys.count(k) > 1})
    if twice:
        raise TaxonomyError(f"a key is given twice in one object: {', '.join(twice)}")
    return dict(pairs)


def _check_derived(dim: str, d: dict, dims: dict) -> None:
    """A derived dimension: one value per message, its source a one-value dimension of the file,
    and every family (families) or every value (values) of the source mapped to exactly one of
    its values."""
    src = d["derived_from"]
    if not isinstance(src, dict) or (isinstance(src.get("families"), dict) == isinstance(src.get("values"), dict)):
        raise TaxonomyError(f"{dim}: derived_from needs a dimension and either a families or a values map")
    source = dims.get(src.get("dimension"))
    if source is None or source["cardinality"] != "one" or d["cardinality"] != "one":
        raise TaxonomyError(f"{dim}: derived_from names a one-value dimension of the file, and {dim} is one-value")
    values = {v["value"] for v in d["values"]}
    if isinstance(src.get("values"), dict):
        _check_value_map(dim, src, values, source)
        return
    families = {v["family"] for v in source["values"]}
    unmapped = sorted(families - set(src["families"]))
    if unmapped:
        raise TaxonomyError(f"{dim}: these families of {src['dimension']} map to nothing: {', '.join(unmapped)}")
    bad = sorted(f for f, side in src["families"].items() if f not in families or side not in values)
    if bad:
        raise TaxonomyError(f"{dim}: derived_from maps an unknown family or to an unknown value: {', '.join(bad)}")


def _check_value_map(dim: str, src: dict, values: set[str], source: dict) -> None:
    """values: {value of dim: [source values under it]}. Every source value exactly once."""
    groups = src["values"]
    bad = sorted(k for k, g in groups.items() if k not in values or not isinstance(g, list)
                 or not all(isinstance(x, str) for x in g))
    if bad:
        raise TaxonomyError(f"{dim}: derived_from.values has an unknown value or a group that is not a list:"
                            f" {', '.join(bad)}")
    known = [v["value"] for v in source["values"]]
    seen: dict[str, list[str]] = {}
    for k, g in groups.items():
        for x in g:
            seen.setdefault(x, []).append(k)
    twice = sorted(f"{x} ({', '.join(ks)})" for x, ks in seen.items() if len(ks) > 1)
    if twice:
        raise TaxonomyError(f"{dim}: these {src['dimension']} values are under more than one {dim}: {'; '.join(twice)}")
    unknown = sorted(x for x in seen if x not in known)
    if unknown:
        raise TaxonomyError(f"{dim}: derived_from.values lists {src['dimension']} values the file does not have:"
                            f" {', '.join(unknown)}")
    unmapped = [x for x in known if x not in seen]
    if unmapped:
        raise TaxonomyError(f"{dim}: these {src['dimension']} values map to no {dim}: {', '.join(unmapped)}")


def mapping(derived_from: dict, source_meta: dict, source_values) -> dict[str, str | None]:
    """{source value: the derived value} for a stored derived_from: by family or by value."""
    if isinstance(derived_from.get("values"), dict):
        inv = {x: k for k, g in derived_from["values"].items() for x in g}
        return {v: inv.get(v) for v in source_values}
    fam = derived_from.get("families") or {}
    return {v: fam.get((source_meta.get(v) or {}).get("family")) for v in source_values}


def in_use(conn: psycopg.Connection, dim: str) -> dict[str, list[str]]:
    """Every value of a dimension that something still uses, with what: 'assignments 12',
    'rule seed-x', 'answer key 3'."""
    uses: dict[str, list[str]] = {}
    for r in conn.execute("select value, count(*)::int as n from assignment where dimension_id = %s"
                          " and status = 'active' group by 1", (dim,)):
        uses.setdefault(r["value"], []).append(f"assignments {r['n']}")
    for r in conn.execute("select action ->> 'value' as value, string_agg(id, ', ' order by id) as ids from rule"
                          " where action ->> 'dimension' = %s and coalesce(action ->> 'value', '') <> '' group by 1",
                          (dim,)):
        uses.setdefault(r["value"], []).append(f"rule {r['ids']}")
    if dim in GOLD_FIELDS:
        for r in conn.execute("select v as value, count(*)::int as n from gold_label, unnest(values) v"
                              " where field = %s group by 1", (dim,)):
            if dim == "route" and r["value"].isdigit():
                continue  # an object id: route's old meaning, read as unset by the answer key
            uses.setdefault(r["value"], []).append(f"answer key {r['n']}")
    return uses


def load(conn: psycopg.Connection, path: Path | str | None = None) -> dict:
    """Load the file into the dimension table, or raise TaxonomyError listing the values still
    in use that the file no longer has. Returns per dimension what changed."""
    dims = read(path)
    with conn.transaction():
        missing = {}
        for dim, d in dims.items():
            values = {v["value"] for v in d["values"]}
            gone = {v: u for v, u in sorted(in_use(conn, dim).items()) if v not in values}
            if gone:
                missing[dim] = gone
        if missing:
            lines = [f"  {dim}: " + "; ".join(f"{v} ({', '.join(u)})" for v, u in gone.items())
                     for dim, gone in missing.items()]
            raise TaxonomyError("refused: these values are in use but not in the file; add them back, or change"
                                " what uses them first:\n" + "\n".join(lines))
        before = {r["id"]: r for r in conn.execute(
            "select id, label, cardinality, allowed, value_meta, description, derived_from from dimension"
            " where id = any(%s)",
            (list(dims),))}
        summary = {}
        for dim, d in dims.items():
            allowed = [v["value"] for v in d["values"]]
            meta = {v["value"]: {"family": v["family"], "label": v["label"], "description": v["description"],
                                 **({"common": int(v["common"])} if v.get("common") else {})}
                    for v in d["values"]}
            old = before.get(dim)
            row = {"label": d["label"], "cardinality": d["cardinality"], "allowed": allowed, "value_meta": meta,
                   "description": d["description"], "derived_from": d["derived_from"]}
            changed = not old or any(old[k] != row[k] for k in row)
            if changed:
                conn.execute(
                    "insert into dimension (id, label, cardinality, allowed, value_meta, description, derived_from)"
                    " values (%s, %s, %s, %s, %s, %s, %s) on conflict (id) do update set label = excluded.label,"
                    " cardinality = excluded.cardinality, allowed = excluded.allowed,"
                    " value_meta = excluded.value_meta, description = excluded.description,"
                    " derived_from = excluded.derived_from",
                    (dim, d["label"], d["cardinality"], Jsonb(allowed), Jsonb(meta), d["description"],
                     Jsonb(d["derived_from"]) if d["derived_from"] is not None else None))
            old_values = list((old or {}).get("allowed") or [])
            summary[dim] = {"values": len(allowed), "changed": changed, "new": not old,
                            "added": [v for v in allowed if v not in old_values],
                            "removed": [v for v in old_values if v not in allowed]}
    return summary


def format_summary(summary: dict) -> str:
    lines = []
    for dim, s in summary.items():
        what = "new" if s["new"] else "updated" if s["changed"] else "unchanged"
        extra = ""
        if not s["new"] and (s["added"] or s["removed"]):
            extra = f" (+{len(s['added'])} −{len(s['removed'])})"
        lines.append(f"  {dim:<8} {s['values']:>3} values  {what}{extra}")
    changed = sum(1 for s in summary.values() if s["changed"])
    return f"taxonomy: {changed} of {len(summary)} dimensions written\n" + "\n".join(lines)
