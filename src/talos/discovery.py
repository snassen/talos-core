"""Discovery review: the drafted systems, candidate binders and the owner's own setup, decided one by one.

The drafts are three files in TALOS_HOME/config/discovery (docs/discovery/README.md; mined from the
owner's mail, so they stay out of the repository: talos.personal): systems.json (the
systems of the owner's employer), candidates.json (new areas, topics, systems and projects) and
my-setup.json (what runs on the Mac). Each item carries ``decision`` (null, accept or reject) and
``correction`` (the fields the owner wants changed).

- ``load`` reads the files into ``discovery_item``, one row per item. It is idempotent: the
  draft's own fields are refreshed, a decision already made is never overwritten, and an item that
  left the file is reported, never deleted. A decision written into a file by hand is taken when
  the row has none yet.
- ``decide`` records the owner's decision and makes the binder. Accepting a system makes an object of kind
  ``system`` (its body: Purpose, How it's used, Evidence, Related systems, How Talos could document
  it, Open questions; its attrs: status, vendor, category, first and last seen; its origin uid
  ``discovery:<key>``). A binder of the same kind and name that already exists (the eleven from
  the vault) is linked and enriched instead: the draft becomes a note on it and only its empty
  attrs are filled. A candidate becomes a binder of its kind, nested under its suggested parent when
  that exists. A setup item becomes a line in a note (one per group) on the binder "My setup (<host>)".
  Rejecting stores the decision only, except "Keep as retired system": a binder with lifecycle
  retired, for history and the dead-mail cleanup. Accepting twice makes nothing new.
- ``bulk`` accepts every undecided active system, or rejects every undecided probably-retired
  system and marks it retired (no binder).
- ``export`` writes the decisions back into the files; the table is the source of truth.

Everything here writes to Talos's own tables only.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from talos import objects, personal


def default_dir() -> Path:
    """Where the owner's drafts are: TALOS_HOME/config/discovery."""
    return personal.home() / "discovery"


SOURCES = ("systems", "candidates", "my-setup")
BINDER_KINDS = ("project", "personal_project", "area", "topic", "system")
DECISIONS = ("accept", "reject")
# What a correction may change. keep_as_binder: a rejected system kept as a retired binder.
CORRECTABLE = ("name", "kind", "description", "status", "note", "keep_as_binder")
STATUSES = ("active", "unclear", "legacy", "probably_retired", "retired", "not_adopted")
EVIDENCE_LABEL = {
    "work_mail_from_system": "Work mail from the system's own senders",
    "work_mail_subject_mentions": "Other work mail naming it in the subject",
    "work_mail_sent_by_him": "Work mail you sent about it",
    "teams_messages": "Teams messages naming it",
    "personal_gmail": "Personal Gmail",
    "last_12_months": "In the last 12 months",
    "last_ops_subject": "Last operational subject (invoice, licence, alert, backup)",
    "assigned_messages": "Messages Talos has assigned to it",
    "last_12m": "In the last 12 months",
    "mail": "Mail",
    "teams": "Teams messages",
    "gmail": "Personal Gmail",
    "first": "First seen",
    "last": "Last seen",
}
SETUP_KIND = {"application": "App", "cli": "CLI tool", "service": "Service", "repo": "Repository",
              "device": "Device", "agent": "Agent"}
BY = "discovery"


class DiscoveryError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _norm(name: str | None) -> str:
    return " ".join(str(name or "").lower().split())


def _bare(name: str | None) -> str:
    """The name without a trailing parenthesis: 'Corporate VPN (existing binder)' → 'corporate vpn'."""
    return _norm(re.sub(r"\s*\([^)]*\)\s*$", "", str(name or "")))


def _empty(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the files

def read(path: Path | str) -> dict:
    """A discovery file, checked: a dict with a list of items, each with a key and a name."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DiscoveryError(f"{path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise DiscoveryError(f"{path}: expected an object with a list of items")
    seen = set()
    for i, item in enumerate(data["items"]):
        if not isinstance(item, dict) or not item.get("key") or not item.get("name"):
            raise DiscoveryError(f"{path}: item {i} has no key or name")
        if item["key"] in seen:
            raise DiscoveryError(f"{path}: the key {item['key']!r} appears twice")
        seen.add(item["key"])
        if item.get("decision") not in (None, *DECISIONS):
            raise DiscoveryError(f"{path}: item {item['key']!r} has decision {item.get('decision')!r}")
    return data


def _path(directory: Path | str | None, source: str) -> Path:
    return Path(directory or default_dir()) / f"{source}.json"


def load(conn: psycopg.Connection, directory: Path | str | None = None) -> dict:
    """Read the three files into discovery_item. Returns a report per source.

    New items are added; the draft fields of known ones are refreshed; a decision already made is
    kept whatever the file says. A decision in the file is taken only for a row without one, and
    then carried out (the binder made) as if the owner had pressed the button."""
    report: dict = {}
    with conn.transaction():
        for source in SOURCES:
            path = _path(directory, source)
            if not path.exists():
                report[source] = {"missing": str(path)}
                continue
            data = read(path)
            meta = {k: v for k, v in data.items() if k != "items"}
            conn.execute(
                "insert into discovery_source (source, path, meta) values (%s, %s, %s)"
                " on conflict (source) do update set path = excluded.path, meta = excluded.meta, loaded_at = now()",
                (source, str(path), Jsonb(meta)))
            r = {"items": len(data["items"]), "new": 0, "updated": 0, "unchanged": 0,
                 "decisions_from_file": 0, "decisions_kept": 0, "gone": []}
            known = {row["item_key"]: row for row in conn.execute(
                "select * from discovery_item where source = %s", (source,))}
            taken = []
            for pos, item in enumerate(data["items"]):
                key = item["key"]
                draft = {k: v for k, v in item.items() if k not in ("decision", "correction")}
                fields = (item.get("kind") or "", item["name"], item.get("status"), pos)
                row = known.pop(key, None)
                if row is None:
                    rid = conn.execute(
                        "insert into discovery_item (source, item_key, kind, name, status, position, payload)"
                        " values (%s, %s, %s, %s, %s, %s, %s) returning id",
                        (source, key, *fields, Jsonb(draft))).fetchone()["id"]
                    r["new"] += 1
                    row = {"id": rid, "decision": None}
                elif (row["kind"], row["name"], row["status"], row["position"]) != fields or row["payload"] != draft:
                    conn.execute("update discovery_item set kind = %s, name = %s, status = %s, position = %s,"
                                 " payload = %s, loaded_at = now() where id = %s", (*fields, Jsonb(draft), row["id"]))
                    r["updated"] += 1
                else:
                    r["unchanged"] += 1
                if item.get("decision"):
                    if row["decision"] is None:
                        conn.execute("update discovery_item set decision = %s, correction = %s, decided_at = now()"
                                     " where id = %s", (item["decision"], Jsonb(item["correction"]) if isinstance(item.get("correction"), dict)
                                      and item["correction"] else None, row["id"]))
                        taken.append(row["id"])
                        r["decisions_from_file"] += 1
                    elif row["decision"] != item["decision"]:
                        r["decisions_kept"] += 1
            r["gone"] = sorted(known)
            for rid in taken:
                _apply(conn, _row(conn, rid))
            report[source] = r
    return report


def format_report(report: dict) -> str:
    lines = []
    for source, r in report.items():
        if "missing" in r:
            lines.append(f"{source}: not loaded, {r['missing']} is not there")
            continue
        lines.append(f"{source}: {r['items']} items: {r['new']} new, {r['updated']} updated, {r['unchanged']} unchanged"
                     + (f"; {r['decisions_from_file']} decisions taken from the file" if r["decisions_from_file"] else "")
                     + (f"; {r['decisions_kept']} decisions kept (the file says otherwise)" if r["decisions_kept"] else "")
                     + (f"; no longer in the file (kept): {', '.join(r['gone'])}" if r["gone"] else ""))
    return "\n".join(lines)


def export(conn: psycopg.Connection, directory: Path | str | None = None, *, out: Path | str | None = None) -> dict:
    """Write the decisions (decision and correction) back into the files, everything else as it was.

    Written into ``out`` when given (same file names), else over the files themselves. Returns
    {source: number of items whose decision or correction changed}."""
    changed = {}
    for source in SOURCES:
        path = _path(directory, source)
        if not path.exists():
            continue
        data = read(path)
        rows = {r["item_key"]: r for r in conn.execute(
            "select item_key, decision, correction from discovery_item where source = %s", (source,))}
        n = 0
        for item in data["items"]:
            row = rows.get(item["key"])
            if row is None:
                continue
            if (item.get("decision"), item.get("correction")) != (row["decision"], row["correction"]):
                n += 1
            item["decision"], item["correction"] = row["decision"], row["correction"]
        target = Path(out) / path.name if out else path
        target.parent.mkdir(parents=True, exist_ok=True)
        # The files' own format (indent 1, UTF-8 as is, no newline at the end), so an export
        # without new decisions changes nothing.
        target.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        changed[source] = n
    return changed


# ---------------------------------------------------------------- reading the table

def _row(conn: psycopg.Connection, item_id: int) -> dict:
    row = conn.execute("select * from discovery_item where id = %s", (item_id,)).fetchone()
    if not row:
        raise DiscoveryError(f"no discovery item {item_id}")
    return row


def items(conn: psycopg.Connection) -> dict:
    """Every item with its binder, and per source the file's header and the progress."""
    rows = conn.execute(
        "select d.id, d.source, d.item_key, d.kind, d.name, d.status, d.position, d.payload, d.decision,"
        " d.correction, d.decided_at, d.object_id, o.name as object_name, o.kind as object_kind"
        " from discovery_item d left join object o on o.id = d.object_id order by d.source, d.position, d.id").fetchall()
    sources = {}
    for s in SOURCES:
        mine = [r for r in rows if r["source"] == s]
        meta = conn.execute("select meta, path, loaded_at from discovery_source where source = %s", (s,)).fetchone()
        sources[s] = {"total": len(mine), "decided": sum(1 for r in mine if r["decision"]),
                      "accepted": sum(1 for r in mine if r["decision"] == "accept"),
                      "rejected": sum(1 for r in mine if r["decision"] == "reject"),
                      "meta": meta["meta"] if meta else {}, "loaded_at": meta["loaded_at"] if meta else None}
    names = {r["item_key"]: r["name"] for r in rows if r["source"] == "systems"}
    for r in rows:
        r["related_names"] = [names.get(k, k) for k in (r["payload"].get("related") or [])]
    return {"sources": sources, "items": rows}


# ---------------------------------------------------------------- deciding

def _correction(c) -> dict | None:
    if c is None:
        return None
    if not isinstance(c, dict):
        raise DiscoveryError("a correction is an object of the fields to change")
    out = {}
    for k, v in c.items():
        if k not in CORRECTABLE:
            raise DiscoveryError(f"a correction may change {', '.join(CORRECTABLE)}; not {k!r}")
        if k == "keep_as_binder":
            if v:
                out[k] = True
            continue
        v = v.strip() if isinstance(v, str) else v
        if _empty(v):
            continue
        if k == "kind" and v not in BINDER_KINDS:
            raise DiscoveryError(f"kind must be one of {', '.join(BINDER_KINDS)}")
        if k == "status" and v not in STATUSES:
            raise DiscoveryError(f"status must be one of {', '.join(STATUSES)}")
        if not isinstance(v, str):
            raise DiscoveryError(f"{k} is text")
        out[k] = v
    return out or None


def decide(conn: psycopg.Connection, item_ids: list[int], decision: str | None, *, correction: dict | None = None,
           keep_retired: bool = False, mark_retired: bool = False) -> list[dict]:
    """Record the owner's decision on each item and carry it out. Returns the items as they are now.

    decision None undoes a decision (a binder made by it stays; archive it on its page). A
    correction's fields are merged into the item's earlier one. On reject, keep_retired keeps the
    system as a binder with lifecycle retired, and mark_retired marks it retired without one."""
    if decision not in (None, *DECISIONS):
        raise DiscoveryError("decision must be accept, reject or null")
    ids = sorted({int(i) for i in item_ids or []})
    if not ids:
        raise DiscoveryError("no items given")
    fix = _correction(correction) or {}
    if decision == "reject" and (keep_retired or mark_retired):
        fix["status"] = "retired"
        if keep_retired:
            fix["keep_as_binder"] = True
    out = []
    with conn.transaction():
        for item_id in ids:
            row = _row(conn, item_id)
            merged = {**(row["correction"] or {}), **fix}
            if decision != "reject" or not keep_retired:
                merged.pop("keep_as_binder", None)
            if keep_retired and row["source"] != "systems":
                raise DiscoveryError("only a system can be kept as a retired binder")
            if fix.get("kind") and row["source"] == "my-setup":
                raise DiscoveryError("a setup item becomes a note; it has no kind to change")
            conn.execute("update discovery_item set decision = %s, correction = %s,"
                         " decided_at = case when %s::text is null then null else now() end where id = %s",
                         (decision, Jsonb(merged) if merged else None, decision, item_id))
            _apply(conn, _row(conn, item_id))
            out.append(_row(conn, item_id))
    return out


def bulk(conn: psycopg.Connection, action: str) -> dict:
    """accept_active: every undecided active system accepted. reject_retired: every undecided
    probably-retired system rejected and marked retired (no binder). Decided items are left alone."""
    if action == "accept_active":
        ids = [r["id"] for r in conn.execute(
            "select id from discovery_item where source = 'systems' and status = 'active' and decision is null")]
        done = decide(conn, ids, "accept") if ids else []
    elif action == "reject_retired":
        ids = [r["id"] for r in conn.execute(
            "select id from discovery_item where source = 'systems' and status = 'probably_retired' and decision is null")]
        done = decide(conn, ids, "reject", mark_retired=True) if ids else []
    else:
        raise DiscoveryError("action must be accept_active or reject_retired")
    return {"action": action, "count": len(done), "ids": [r["id"] for r in done]}


def _effective(row: dict) -> dict:
    """The draft as the owner corrected it: name, kind, description, status and note."""
    p, c = row["payload"], row["correction"] or {}
    description = {"systems": p.get("used_for"), "candidates": p.get("why"), "my-setup": p.get("what")}[row["source"]]
    kind = row["kind"] if row["source"] != "my-setup" else None
    return {"name": c.get("name") or row["name"], "kind": c.get("kind") or kind,
            "description": c.get("description") or description, "status": c.get("status") or row["status"],
            "note": c.get("note"), "keep_as_binder": bool(c.get("keep_as_binder"))}


def _apply(conn: psycopg.Connection, row: dict) -> int | None:
    """Carry out the row's decision: make or enrich its binder. Idempotent."""
    eff = _effective(row)
    wants_binder = row["decision"] == "accept" or (row["decision"] == "reject" and eff["keep_as_binder"])
    if row["source"] == "my-setup":
        oid = _setup_binder(conn) if wants_binder else row["object_id"]
        if oid:
            _setup_notes(conn, oid, row["payload"].get("group") or SETUP_KIND.get(row["kind"], row["kind"]))
    elif not wants_binder:
        return row["object_id"]
    else:
        oid = _binder(conn, row, eff)
    if oid and oid != row["object_id"]:
        conn.execute("update discovery_item set object_id = %s where id = %s", (oid, row["id"]))
    return oid


# ---------------------------------------------------------------- binders

def _find_existing(conn: psycopg.Connection, kind: str, name: str, existing_id=None) -> int | None:
    """A binder of this kind that is the same thing: the draft's existing_object_id when it is one of
    this kind with a like name, else one whose name is the draft's (with or without its parenthesis)."""
    if existing_id:
        try:
            got = conn.execute("select id, kind, name from object where id = %s", (int(existing_id),)).fetchone()
        except (TypeError, ValueError):
            got = None
        if got and got["kind"] == kind:
            a, b = _norm(got["name"]), _norm(name)
            if a and b and (a in b or b in a):
                return got["id"]
    wanted = {_norm(name), _bare(name)} - {""}
    for r in conn.execute("select id, name from object where kind = %s order by archived, id", (kind,)):
        if _norm(r["name"]) in wanted:
            return r["id"]
    return None


def _binder(conn: psycopg.Connection, row: dict, eff: dict) -> int:
    uid = f"discovery:{row['item_key']}"
    p = row["payload"]
    kind = eff["kind"] if eff["kind"] in BINDER_KINDS else "system"
    retired = eff["keep_as_binder"]
    body = _system_body(conn, row, eff) if row["source"] == "systems" else _candidate_body(row, eff)
    attrs = _attrs(row, eff, retired)

    mine = conn.execute("select id from object where origin ->> 'uid' = %s", (uid,)).fetchone()
    if mine:
        return mine["id"]
    if row["object_id"] and objects.get(conn, row["object_id"]):
        return row["object_id"]
    existing = _find_existing(conn, kind, eff["name"], p.get("existing_object_id"))
    if existing:
        _enrich(conn, existing, uid, row, eff, body, attrs)
        return existing
    oid = objects.create(conn, kind, eff["name"], description=eff["description"], attrs=attrs)
    objects.set_body(conn, oid, body)
    conn.execute("update object set origin = %s where id = %s", (Jsonb({
        "source": BY, "uid": uid, "file": f"config/discovery/{row['source']}.json", "key": row["item_key"],
        "imported_at": _now().isoformat()}), oid))
    parent = p.get("suggested_parent")
    if row["source"] == "candidates" and parent:
        for r in conn.execute("select id, name from object where not archived and kind = any(%s) order by id",
                              (list(BINDER_KINDS),)):
            if _bare(r["name"]) == _bare(parent) and r["id"] != oid:
                objects.add(conn, r["id"], [oid])
                break
    return oid


def _attrs(row: dict, eff: dict, retired: bool) -> dict:
    p = row["payload"]
    ev = p.get("evidence") or {}
    attrs = {"status": eff["status"], "vendor": p.get("vendor"), "category": p.get("category"),
             "first_seen": p.get("first_seen") or ev.get("first"), "last_seen": p.get("last_seen") or ev.get("last"),
             "lifecycle": "retired" if retired else "active", "discovery_key": row["item_key"]}
    if row["source"] == "candidates":
        attrs.update({"sphere": p.get("sphere"), "confidence": p.get("confidence"),
                      "suggested_parent": p.get("suggested_parent"), "rank": p.get("rank")})
    return {k: v for k, v in attrs.items() if not _empty(v)}


def _enrich(conn: psycopg.Connection, oid: int, uid: str, row: dict, eff: dict, body: str, attrs: dict) -> None:
    """Link a binder that already exists: the draft as a note on it, its empty attrs filled."""
    obj = objects.get(conn, oid)
    have = dict(obj["attrs"] or {})
    filled = {k: v for k, v in attrs.items() if _empty(have.get(k))}
    if filled:
        conn.execute("update object set attrs = %s, updated_at = now() where id = %s", (Jsonb({**have, **filled}), oid))
    if not obj["description"] and eff["description"]:
        objects.describe(conn, oid, eff["description"])
    if not conn.execute("select 1 from note where object_id = %s and origin ->> 'uid' = %s", (oid, uid)).fetchone():
        _note(conn, oid, f"Discovery draft: {eff['name']}", body, {"source": BY, "uid": uid, "key": row["item_key"]},
              kind="discovery")


def _note(conn: psycopg.Connection, oid: int, title: str, body: str, origin: dict, *, kind: str) -> int:
    nid = conn.execute("insert into entity (kind) values ('note') returning id").fetchone()["id"]
    conn.execute("insert into note (id, object_id, kind, title, body, origin) values (%s, %s, %s, %s, %s, %s)",
                 (nid, oid, kind, title, body, Jsonb(origin)))
    return nid


def _bullets(values) -> list[str]:
    return [f"- {v}" for v in values or [] if not _empty(v)]


def _section(title: str, lines: list[str]) -> list[str]:
    return [f"## {title}", "", *(lines or ["(none in the draft)"]), ""]


def _evidence(ev: dict) -> list[str]:
    # jsonb keeps no key order, so the evidence is listed in EVIDENCE_LABEL's order, the rest after.
    order = list(EVIDENCE_LABEL)
    keys = sorted(ev or {}, key=lambda k: (order.index(k) if k in order else len(order), k))
    return [f"- {EVIDENCE_LABEL.get(k, k.replace('_', ' ').capitalize())}: "
            f"{f'{ev[k]:,}'.replace(',', ' ') if isinstance(ev[k], int) else ev[k]}"
            for k in keys if ev[k] not in (None, "")]


def _system_body(conn: psycopg.Connection, row: dict, eff: dict) -> str:
    p = row["payload"]
    meta = conn.execute("select meta from discovery_source where source = 'systems'").fetchone()
    status_text = ((meta or {}).get("meta") or {}).get("status_values", {}).get(eff["status"] or "", "")
    used = [f"- Category: {p['category']}" if p.get("category") else None,
            f"- Vendor: {p['vendor']}" if p.get("vendor") else None,
            f"- Status: {(eff['status'] or 'unknown').replace('_', ' ')}" + (f" ({status_text})" if status_text else ""),
            f"- Seen: {p.get('first_seen') or '?'} to {p.get('last_seen') or '?'}",
            f"- Last mail from its own senders: {p['last_mail_from_own_senders']}"
            if p.get("last_mail_from_own_senders") else None]
    used = [x for x in used if x]
    if p.get("examples"):
        used += ["", "Example subjects:", "", *_bullets(f"“{x}”" for x in p["examples"])]
    if p.get("noise"):
        used += ["", f"Noise: {p['noise'].get('n', '?')} {p['noise'].get('what', '')}".rstrip()]
    evidence = _evidence(p.get("evidence"))
    if p.get("cleanup"):
        evidence += ["", f"Cleanup candidate: {p['cleanup'].get('mail', '?')} mails from these senders:", "",
                     *_bullets(p["cleanup"].get("senders"))]
    names = {r["item_key"]: r["name"] for r in conn.execute(
        "select item_key, name from discovery_item where source = 'systems' and item_key = any(%s)",
        (list(p.get("related") or []),))}
    lines = [*_section("Purpose", [eff["description"] or ""]), *_section("How it's used", used),
             *_section("Evidence", evidence),
             *_section("Related systems", _bullets(names.get(k, k) for k in p.get("related") or [])),
             *_section("How Talos could document it", [p["suggested_collector"]] if p.get("suggested_collector") else []),
             *_section("Open questions", _bullets(p.get("open_questions")))]
    if eff["note"]:
        lines += _section("Your note", [eff["note"]])
    lines += [(f"_From the discovery draft of {((meta or {}).get('meta') or {}).get('generated', '?')}"
               f" (config/discovery/systems.json, key {row['item_key']})._")]
    return "\n".join(lines)


def _candidate_body(row: dict, eff: dict) -> str:
    p = row["payload"]
    place = [f"- Suggested parent: {p['suggested_parent']}" if p.get("suggested_parent") else None,
             f"- Sphere: {p['sphere']}" if p.get("sphere") else None,
             f"- Confidence: {p['confidence']}" if p.get("confidence") else None,
             f"- Rank among the candidates: {p['rank']}" if p.get("rank") else None]
    lines = [*_section("Purpose", [eff["description"] or ""]), *_section("Evidence", _evidence(p.get("evidence"))),
             *_section("Where it fits", [x for x in place if x])]
    if eff["note"]:
        lines += _section("Your note", [eff["note"]])
    lines += [f"_From the discovery candidates (config/discovery/candidates.json, key {row['item_key']})._"]
    return "\n".join(lines)


# ---------------------------------------------------------------- my setup

def _setup_meta(conn: psycopg.Connection) -> dict:
    got = conn.execute("select meta from discovery_source where source = 'my-setup'").fetchone()
    return (got or {}).get("meta") or {}


def _setup_binder(conn: psycopg.Connection) -> int:
    meta = _setup_meta(conn)
    host = str(meta.get("host") or "mac")
    uid = f"discovery:my-setup:{host}"
    got = conn.execute("select id from object where origin ->> 'uid' = %s", (uid,)).fetchone()
    if got:
        return got["id"]
    name = f"My setup ({host.capitalize()})"
    existing = _find_existing(conn, "topic", name)
    if existing:
        return existing
    description = f"What runs on {host.capitalize()}: apps, CLI tools, services, repos, devices and agents."
    oid = objects.create(conn, "topic", name, description=description,
                         attrs={"host": host, "lifecycle": "active", "discovery_key": "my-setup"})
    body = [*_section("Purpose", [description]),
            *_section("How it was gathered", [meta["method"]] if meta.get("method") else []),
            *_section("Open questions", _bullets(meta.get("open_questions"))),
            "Each group of accepted items is a note on this binder."]
    objects.set_body(conn, oid, "\n".join(body))
    conn.execute("update object set origin = %s where id = %s", (Jsonb({
        "source": BY, "uid": uid, "file": "config/discovery/my-setup.json", "imported_at": _now().isoformat()}), oid))
    return oid


def _setup_line(p: dict) -> str:
    extra = [x for x in (p.get("os"), p.get("remote")) if x]
    return (f"- **{p['name']}** ({SETUP_KIND.get(p.get('kind'), p.get('kind') or '?')})"
            + (f": {p['what']}" if p.get("what") else "") + (f" · {' · '.join(extra)}" if extra else ""))


def _setup_notes(conn: psycopg.Connection, oid: int, group: str) -> None:
    """The group's note lists its accepted items. Rewritten while it is as Talos wrote it; once the
    owner has edited it, a newly accepted item is only added at the end."""
    rows = conn.execute(
        "select payload from discovery_item where source = 'my-setup' and decision = 'accept'"
        " and coalesce(payload ->> 'group', kind) = %s order by position", (group,)).fetchall()
    lines = [_setup_line(r["payload"]) for r in rows]
    uid = f"discovery:my-setup:group:{group}"
    note = conn.execute("select id, body, origin from note where object_id = %s and origin ->> 'uid' = %s",
                        (oid, uid)).fetchone()
    if note is None:
        if lines:
            body = "\n".join(lines)
            _note(conn, oid, group, body, {"source": BY, "uid": uid, "hash": _sha(body)}, kind="setup")
        return
    edited = _sha(note["body"]) != note["origin"].get("hash")
    if not edited:
        body = "\n".join(lines)
    else:
        body = note["body"].rstrip("\n")
        body = "\n".join([body, *[x for x in lines if x not in body.splitlines()]])
    if body != note["body"]:
        # An edited note keeps its old hash, so it stays edited and is never rewritten.
        origin = note["origin"] if edited else {**note["origin"], "hash": _sha(body)}
        conn.execute("update note set body = %s, origin = %s, updated_at = now() where id = %s",
                     (body, Jsonb(origin), note["id"]))
