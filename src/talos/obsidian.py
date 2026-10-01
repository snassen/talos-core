"""Lift the Obsidian vault "Talos" into Talos Web (docs/obsidian-mapping.md, option B).

(The module is called obsidian, not vault: talos.vault is the store of original messages.)

The vault is read, never written. What it holds becomes Talos Web's own records:

- **Binders** (`_home.md` with `type: project | area | topic | system`) become objects. A
  project under `Personal Projects/` is a personal_project; a binder under `Archive/` is
  archived. The `_home.md` body (without its embedded views) is the object's body, the
  system State note's fields and the canvases go into attrs, and `parent`, `areas`,
  `topics` and `projects` become edges with source `import:vault`.
- **Work items** (`type: work-item`) go through `talos.work.create`, so each has its
  created event `by='import:vault'`. `home` is the binder object; `related` links become
  related edges; the mail or chat a work item came from becomes an `about` edge to the
  message, found by the Message-ID in the Spark link or by the Teams chat and message id.
  Nothing is guessed: what does not resolve is reported.
- **Notes** (binder notes, incidents, sources, recent state, routing receipts, signals,
  the trial log and the `_system` notes) become rows in `note`, attached to their binder.

Re-running is safe. Binders and work items are keyed on the vault `uid`, notes on their
path. A record is updated only when its file changed since the import **and** Talos Web
has not changed it: each record's origin keeps the hash of the file and a fingerprint of
the fields as imported, and a work item also counts any event by someone other than
`import:vault`. What the owner changed in Talos Web wins, and is reported as "kept your
change". Nothing is ever deleted; records whose file is gone are only reported.

Skipped, with the reason in the report: Bases (views, rebuilt natively), `_template` and
`_templates` folders, `.obsidian`, the History change feed, the vault's dashboards and
agent entry points, and iCloud placeholders that are not downloaded (never taken as
deleted).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlparse

import psycopg
import yaml
from psycopg.types.json import Jsonb

from talos import objects, work

DEFAULT_ROOT = Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents/Talos"
BY = "import:vault"
# Part of every record's content hash: bump it when the mapping changes, so a re-run
# re-applies the new mapping to everything Talos Web has not changed.
VERSION = 2
BINDER_TYPES = ("project", "area", "topic", "system")
STATUS_MAP = {"todo": "next"}          # the vault's odd statuses, mapped and reported
TEMPLATE_DIRS = ("_template", "_templates")
# Vault machinery that has no place in Talos Web: dashboards and agent entry points.
MACHINERY = {
    "Home.md": "the vault's home dashboard (a projection; Talos Web has its own Overview)",
    "CLAUDE.md": "the agent entry point of the vault",
    "AGENTS.md": "the agent entry point of the vault",
    "Inbox/_home.md": "the Inbox board page (a projection; Talos Web has its own Inbox)",
    "Archive/README.md": "instructions for archiving inside Obsidian",
    "_system/History/Changes.md": "generated from the History change feed, rewritten on every snapshot",
}
NOTE_KINDS = {
    "routing-receipt": "receipt", "research-note": "research", "system-note": "system",
    "driver-note": "driver", "decision": "decision", "incident": "incident", "reference": "reference",
    "observation": "observation", "long-job": "long-job", "system-state": "state",
}
STATE_FIELDS = ("health", "version", "update-available", "security-findings", "observed-at", "source-link")
WORK_FIELDS = ("title", "status", "home_id", "focus", "due", "review_after", "body")

_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
_EMBED_LINE = re.compile(r"^\s*!\[\[[^\]]*\]\]\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_TEAMS_ID = re.compile(r"^(19:[^/\s]+)/(\d+)$")


# ---------------------------------------------------------------- the report

@dataclass
class Report:
    root: str
    dry_run: bool
    started_at: str
    counts: dict = field(default_factory=lambda: {k: {"created": 0, "updated": 0, "unchanged": 0, "kept": 0}
                                                  for k in ("binder", "work_item", "note")})
    kinds: dict = field(default_factory=dict)            # what was imported, by kind
    kept_your_change: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    placeholders: list = field(default_factory=list)
    status_mapped: list = field(default_factory=list)
    links: dict = field(default_factory=lambda: {"linked": [], "not_found": [], "no_link": []})
    warnings: list = field(default_factory=list)
    missing_from_vault: list = field(default_factory=list)

    def count(self, what: str, outcome: str, kind: str | None = None) -> None:
        self.counts[what][outcome] += 1
        if kind:
            self.kinds.setdefault(what, {}).setdefault(kind, 0)
            self.kinds[what][kind] += 1

    def skip(self, path: str, reason: str) -> None:
        self.skipped.append({"path": path, "reason": reason})

    def warn(self, path: str, warning: str) -> None:
        self.warnings.append({"path": path, "warning": warning})

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        by_reason: dict[str, int] = {}
        for s in self.skipped:
            by_reason[s["reason"]] = by_reason.get(s["reason"], 0) + 1
        d["skipped_by_reason"] = by_reason
        d["links_summary"] = {k: len(v) for k, v in self.links.items()}
        return d


def format_report(r: dict) -> str:
    """The report as readable text."""
    out = [f"Vault import {'(DRY RUN, nothing written) ' if r['dry_run'] else ''}from {r['root']}", ""]
    for what, c in r["counts"].items():
        kinds = ", ".join(f"{k} {n}" for k, n in sorted(r["kinds"].get(what, {}).items()))
        out.append(f"{what + 's':<11} created {c['created']}, updated {c['updated']}, unchanged {c['unchanged']},"
                   f" kept your change {c['kept']}" + (f"   [{kinds}]" if kinds else ""))
    ls = r["links_summary"]
    out.append(f"message links linked {ls['linked']}, not found {ls['not_found']}, no link {ls['no_link']}")
    if r["kept_your_change"]:
        out += ["", "Kept your change (edited in Talos Web; the vault's newer version was not applied):"]
        out += [f"  - {p}" for p in r["kept_your_change"]]
    if r["status_mapped"]:
        out += ["", "Statuses mapped:"]
        out += [f"  - {s['path']}: {s['from']} -> {s['to']}" for s in r["status_mapped"]]
    if r["links"]["linked"]:
        out += ["", "Linked to their message:"]
        out += [f"  - {x['file']} -> {len(x['message_ids'])} message(s) by {x['via']}" for x in r["links"]["linked"]]
    if r["links"]["not_found"]:
        out += ["", "Message not found:"]
        out += [f"  - {x['file']}: {x['reason']}" for x in r["links"]["not_found"]]
    if r["links"]["no_link"]:
        out += ["", "No source link:"] + [f"  - {x}" for x in r["links"]["no_link"]]
    if r["skipped"]:
        out += ["", "Skipped:"] + [f"  - {n} × {reason}" for reason, n in r["skipped_by_reason"].items()]
        out += [f"      {s['path']}" for s in r["skipped"]
                if not s["path"].endswith(".base") and s["reason"] != "template folder"]
    if r["placeholders"]:
        out += ["", "iCloud placeholders (not downloaded; not imported, not taken as deleted):"]
        out += [f"  - {p}" for p in r["placeholders"]]
    if r["warnings"]:
        out += ["", "Warnings:"] + [f"  - {w['path']}: {w['warning']}" for w in r["warnings"]]
    if r["missing_from_vault"]:
        out += ["", "In Talos Web but no longer in the vault (kept):"]
        out += [f"  - {m['what']} {m['path']}" for m in r["missing_from_vault"]]
    return "\n".join(out)


# ---------------------------------------------------------------- reading the vault

@dataclass
class Doc:
    rel: str                 # vault-relative POSIX path
    raw: bytes
    fm: dict
    body: str                # Markdown after the frontmatter

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.raw + b"\0talos.obsidian/%d" % VERSION).hexdigest()

    @property
    def type(self) -> str | None:
        t = self.fm.get("type")
        return str(t).strip() if t else None


@dataclass
class Scan:
    docs: dict = field(default_factory=dict)        # rel -> Doc
    canvases: dict = field(default_factory=dict)    # rel -> parsed JSON


def _split(text: str) -> tuple[str | None, str]:
    if not text.startswith("---"):
        return None, text
    m = re.match(r"^---[ \t]*\r?\n(.*?)(?:\r?\n)?^---[ \t]*(?:\r?\n|$)", text, re.S | re.M)
    if not m:
        return None, text
    return m.group(1), text[m.end():]


def scan(root: Path, report: Report) -> Scan:
    """Walk the vault read-only: parse the Markdown, read the canvases, report the rest."""
    s = Scan()
    for dirpath, dirnames, filenames in os.walk(root):
        here = PurePosixPath(Path(dirpath).relative_to(root).as_posix())
        keep = []
        for d in sorted(dirnames):
            rel = str(here / d) if str(here) != "." else d
            if d == ".obsidian":
                report.skip(rel + "/", "Obsidian settings and plugins (.obsidian)")
            elif d in TEMPLATE_DIRS:
                report.skip(rel + "/", "template folder")
            elif d.startswith("."):
                report.skip(rel + "/", "hidden folder")
            else:
                keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            rel = str(here / name) if str(here) != "." else name
            path = Path(dirpath) / name
            if name.endswith(".icloud"):
                real = name[1:-len(".icloud")] if name.startswith(".") else name[:-len(".icloud")]
                shown = str(here / real) if str(here) != "." else real
                report.placeholders.append(shown)
                report.warn(shown, "iCloud placeholder, not downloaded; open the vault on this Mac and re-run")
                continue
            if name.startswith("."):
                continue                                         # .DS_Store and friends
            if name.endswith(".base"):
                report.skip(rel, "Base (a view; Talos Web rebuilds its views natively)")
                continue
            if name == "changes.jsonl":
                report.skip(rel, "History change feed (history of the vault, not state)")
                continue
            if rel in MACHINERY:
                report.skip(rel, MACHINERY[rel])
                continue
            try:
                raw = path.read_bytes()
            except OSError as exc:
                report.warn(rel, f"could not be read ({type(exc).__name__}); treated as a placeholder")
                report.placeholders.append(rel)
                continue
            if not raw and name.endswith((".md", ".canvas")):
                report.placeholders.append(rel)
                report.warn(rel, "empty file (likely an iCloud stub); not imported, not taken as deleted")
                continue
            if name.endswith(".canvas"):
                try:
                    s.canvases[rel] = json.loads(raw)
                except ValueError:
                    report.warn(rel, "canvas is not valid JSON; not imported")
                continue
            if not name.endswith(".md"):
                report.skip(rel, "not a Markdown note (attachment or other file)")
                continue
            text = raw.decode("utf-8", errors="replace")
            head, body = _split(text)
            fm: dict = {}
            if head is not None:
                try:
                    fm = yaml.safe_load(head) or {}
                    if not isinstance(fm, dict):
                        raise ValueError("frontmatter is not a mapping")
                except (yaml.YAMLError, ValueError) as exc:
                    report.warn(rel, f"unparseable frontmatter ({str(exc).splitlines()[0]}); not imported")
                    report.skip(rel, "unparseable frontmatter")
                    continue
            s.docs[rel] = Doc(rel, raw, fm, body)
    return s


# ---------------------------------------------------------------- small helpers

def _links(value) -> list[str]:
    """Wikilink targets in a frontmatter value (a string, a list, or nothing)."""
    items = value if isinstance(value, list) else [value] if value else []
    out = []
    for v in items:
        found = _targets(str(v))
        out += found if found else ([str(v).strip()] if str(v).strip() else [])
    return out


def _targets(text: str) -> list[str]:
    """Wikilink targets in Markdown text. In a table the pipe is escaped: [[a/b\\|B]]."""
    return [t.strip().rstrip("\\").strip() for t in _WIKILINK.findall(text)]


def _title(doc: Doc) -> str:
    for line in doc.body.splitlines():
        if line.strip():
            m = _HEADING.match(line)
            if m and len(m.group(1)) == 1 and m.group(2).strip():
                return m.group(2).strip()
            break
    return PurePosixPath(doc.rel).stem


def _without_h1(body: str) -> str:
    lines = body.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and (m := _HEADING.match(lines[i])) and len(m.group(1)) == 1:
        lines = lines[i + 1:]
    return "\n".join(lines).strip() + "\n" if any(x.strip() for x in lines) else ""


def _binder_body(body: str) -> str:
    """The _home body without embedded views; a section left empty by that goes too."""
    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in _without_h1(body).splitlines():
        if _HEADING.match(line):
            sections.append((line, []))
        else:
            sections[-1][1].append(line)
    out = []
    for heading, lines in sections:
        kept = [x for x in lines if not _EMBED_LINE.match(x)]
        had_embed = len(kept) != len(lines)
        if had_embed and not any(x.strip() for x in kept):
            continue
        if heading:
            out.append(heading)
        out += kept
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return text + "\n" if text else ""


def _section(body: str, name: str) -> str | None:
    lines, inside, got = body.splitlines(), False, []
    for line in lines:
        m = _HEADING.match(line)
        if m:
            if inside:
                break
            inside = m.group(2).strip().lower() == name.lower()
            continue
        if inside:
            got.append(line)
    text = "\n".join(got).strip()
    return text or None


def _jsonable(v):
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    return v


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _fingerprint(values: dict) -> str:
    return hashlib.sha256(json.dumps(_jsonable(values), sort_keys=True, default=str).encode()).hexdigest()


def _when(v) -> datetime | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=UTC)
    try:
        d = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _day(v) -> date | None | str:
    """A date, None, or the original string when it is not a date (for a warning)."""
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v).strip()[:10])
    except ValueError:
        return str(v)


def _bool(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "yes", "1")


def _text(v) -> str | None:
    if v in (None, ""):
        return None
    return v.isoformat() if isinstance(v, datetime | date) else str(v).strip() or None


# ---------------------------------------------------------------- link resolution

class Resolver:
    """Obsidian wikilinks to vault paths: by path first, then by a unique file name."""

    def __init__(self, docs: dict):
        self.paths = {rel[:-3]: rel for rel in docs if rel.endswith(".md")}
        self.by_name: dict[str, list[str]] = {}
        for rel in self.paths.values():
            self.by_name.setdefault(PurePosixPath(rel).stem.lower(), []).append(rel)

    def path(self, target: str) -> str | None:
        t = target.strip().lstrip("/")
        if t.endswith(".md"):
            t = t[:-3]
        if t in self.paths:
            return self.paths[t]
        hits = self.by_name.get(PurePosixPath(t).name.lower(), [])
        if len(hits) == 1:
            return hits[0]
        return None


def spark_token(link: str | None) -> dict | None:
    """Decode a Spark deep link's token: base64 of 'A:<account>;ID:<Message-ID>;<n>'."""
    if not link or "token=" not in str(link):
        return None
    try:
        token = parse_qs(urlparse(str(link)).query).get("token", [None])[0]
    except ValueError:
        return None
    if not token:
        return None
    token = re.sub(r"\s", "", unquote(token))
    try:
        text = base64.b64decode(token + "=" * (-len(token) % 4)).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return None
    m = re.match(r"^A:(?P<account>[^;]*);ID:(?P<id>.*);(?P<n>[^;]*)$", text, re.S)
    if not m:
        return None
    return {"account": m.group("account").strip(), "message_id": m.group("id").strip().strip("<>")}


def _teams_ids(source_id, link) -> tuple[str, str] | None:
    m = _TEAMS_ID.match(str(source_id or "").strip())
    if m:
        return m.group(1), m.group(2)
    if link and "teams.microsoft.com/l/message/" in str(link):
        parts = urlparse(str(link)).path.split("/")
        if len(parts) >= 5:
            return unquote(parts[3]), unquote(parts[4])
    return None


def _accounts_for(conn, addresses: list[str]) -> list[str]:
    addrs = [a.lower() for a in addresses if a]
    if not addrs:
        return []
    rows = conn.execute(
        "select account_id as id from my_address where address = any(%s) and account_id is not null"
        " union select id from account where lower(address) = any(%s)", (addrs, addrs)).fetchall()
    return [r["id"] for r in rows]


def find_messages(conn, fm: dict) -> tuple[list[int], str | None, str | None]:
    """The messages a work item came from: (ids, how it was found, why not)."""
    link, source_id = fm.get("source-link"), fm.get("source-id")
    teams = _teams_ids(source_id, link)
    if teams:
        chat, msg = teams
        rows = conn.execute("select id from message where provider_key = %s order by id",
                            (f"chat:{chat}:{msg}",)).fetchall()
        if rows:
            return [r["id"] for r in rows], "Teams chat and message id", None
        if not spark_token(link):
            return [], None, f"Teams message {chat}/{msg} is not in Talos"
    token = spark_token(link)
    if token:
        mid = token["message_id"]
        preferred = _accounts_for(conn, [token["account"], str(fm.get("source-account") or "")])
        rows = conn.execute("select id, account_id from message where rfc_message_id = %s order by id",
                            (mid,)).fetchall()
        if not rows:
            rows = conn.execute("select id, account_id from message where lower(rfc_message_id) = lower(%s)"
                                " order by id", (mid,)).fetchall()
        if rows:
            same = [r["id"] for r in rows if r["account_id"] in preferred]
            return (same or [r["id"] for r in rows]), "Spark link (Message-ID)", None
        if token["account"] == "personal-team" or mid.endswith("@Spark"):
            return [], None, "a Spark meeting summary, not a message (Spark keeps it)"
        return [], None, f"Message-ID <{mid}> is not in Talos"
    if link:
        return [], None, "source link is not a Spark or Teams link"
    return [], None, None


# ---------------------------------------------------------------- the import

class Importer:
    def __init__(self, conn: psycopg.Connection, root: Path, report: Report, now: datetime):
        self.conn, self.root, self.r, self.now = conn, root, report, now
        self.s = scan(root, report)
        self.links = Resolver(self.s.docs)
        self.binder_dirs: dict[str, str] = {}      # folder -> _home.md path
        self.ids: dict[str, int] = {}              # vault path -> entity id (binders, items, notes)
        self.touched: set[str] = set()             # binders and items created or updated in this run
        self.seen_uids: dict[str, set] = {"object": set(), "work_item": set()}
        self.seen_notes: set[str] = set()

    # -- classification

    def classify(self) -> tuple[list[Doc], list[Doc], list[Doc]]:
        binders, items, notes = [], [], []
        for rel, doc in self.s.docs.items():
            name = PurePosixPath(rel).name
            if doc.type == "work-item":
                items.append(doc)
            elif name == "_home.md" and doc.type in BINDER_TYPES:
                binders.append(doc)
                self.binder_dirs[str(PurePosixPath(rel).parent)] = rel
            elif doc.type in BINDER_TYPES:
                self.r.warn(rel, f"type {doc.type} outside a _home.md; imported as a note")
                notes.append(doc)
            else:
                notes.append(doc)
        return binders, items, notes

    def binder_of(self, rel: str) -> str | None:
        p = PurePosixPath(rel).parent
        while str(p) not in (".", ""):
            if str(p) in self.binder_dirs:
                return self.binder_dirs[str(p)]
            p = p.parent
        return None

    def target(self, link: str) -> str | None:
        """A wikilink to the vault path of a binder's _home, a work item or a note."""
        rel = self.links.path(link)
        if rel is None:
            # A link to a binder by its folder or name ([[Security]]).
            hits = [h for d, h in self.binder_dirs.items()
                    if PurePosixPath(d).name.lower() == link.strip().lower()
                    or str(self.s.docs[h].fm.get("name") or "").strip().lower() == link.strip().lower()]
            rel = hits[0] if len(hits) == 1 else None
        return rel

    # -- binders

    def binder_fields(self, doc: Doc) -> dict:
        rel, fm = doc.rel, doc.fm
        folder = PurePosixPath(rel).parent
        in_archive = folder.parts[:1] == ("Archive",)
        kind = doc.type
        if kind == "project" and "Personal Projects" in folder.parts:
            kind = "personal_project"
        attrs = {"vault_type": doc.type, "lifecycle": _text(fm.get("lifecycle")), "created": _text(fm.get("created"))}
        extra = {k: _jsonable(v) for k, v in fm.items()
                 if k not in ("type", "name", "uid", "lifecycle", "created", "parent", "areas", "topics", "projects")}
        if extra:
            attrs["frontmatter"] = extra
        state = self.s.docs.get(str(folder / "State" / "current.md"))
        if state:
            attrs["state"] = _clean({k.replace("-", "_"): _jsonable(state.fm.get(k)) for k in STATE_FIELDS})
            attrs["state"]["note"] = _without_h1(state.body).strip() or None
            attrs["state"] = _clean(attrs["state"])
            self.seen_notes.add(state.rel)
        canvases = {PurePosixPath(c).name: data for c, data in self.s.canvases.items()
                    if PurePosixPath(c).parent == folder}
        if canvases:
            attrs["canvases"] = canvases
        body = _binder_body(doc.body)
        return {"kind": kind, "name": str(fm.get("name") or "").strip() or folder.name,
                "description": _section(body, "Purpose"), "body": body, "attrs": _clean(attrs),
                "archived": in_archive or str(fm.get("lifecycle") or "").strip() == "archived"}

    def import_binder(self, doc: Doc) -> None:
        rel, fm = doc.rel, doc.fm
        uid = _text(fm.get("uid"))
        if not uid:
            uid = f"vault-path:{rel}"
            self.r.warn(rel, "binder has no uid; keyed on its path")
        if uid in self.seen_uids["object"]:
            self.r.warn(rel, f"duplicate uid {uid}; this copy was not imported")
            self.r.skip(rel, "duplicate uid")
            return
        self.seen_uids["object"].add(uid)
        f = self.binder_fields(doc)
        # The _home file, its State/current.md and its canvases together make the binder.
        content_hash = _fingerprint({"home": doc.hash, "attrs": f["attrs"]})
        row = self.conn.execute("select * from object where origin ->> 'uid' = %s", (uid,)).fetchone()
        if row:
            self.ids[rel] = row["id"]
            if row["origin"].get("hash") == content_hash:
                self.r.count("binder", "unchanged", f["kind"])
                return
            if _fingerprint(self._object_values(row)) != row["origin"].get("fields_hash"):
                self.r.count("binder", "kept", f["kind"])
                self.r.kept_your_change.append(rel)
                return
            self.conn.execute(
                "update object set kind = %s, name = %s, description = %s, body = %s, attrs = %s, archived = %s,"
                " updated_at = now() where id = %s",
                (f["kind"], f["name"], f["description"], f["body"], Jsonb(f["attrs"]), f["archived"], row["id"]))
            outcome = "updated"
        else:
            oid = objects.create(self.conn, f["kind"], f["name"], description=f["description"], attrs=f["attrs"])
            self.conn.execute("update object set body = %s, archived = %s where id = %s",
                              (f["body"], f["archived"], oid))
            self.ids[rel] = oid
            outcome = "created"
        oid = self.ids[rel]
        new = self.conn.execute("select * from object where id = %s", (oid,)).fetchone()
        origin = {"source": "obsidian", "path": rel, "uid": uid, "hash": content_hash,
                  "fields_hash": _fingerprint(self._object_values(new)), "imported_at": self.now.isoformat()}
        self.conn.execute("update object set origin = %s where id = %s", (Jsonb(origin), oid))
        self.touched.add(rel)
        self.r.count("binder", outcome, f["kind"])

    @staticmethod
    def _object_values(row: dict) -> dict:
        return {k: row[k] for k in ("kind", "name", "description", "body", "attrs", "archived")}

    def binder_edges(self, doc: Doc) -> None:
        if doc.rel not in self.touched:
            return
        oid, fm = self.ids[doc.rel], doc.fm
        self.conn.execute("delete from edge where src = %s and source = %s and rel in ('child_of', 'related')",
                          (oid, BY))
        wanted: list[tuple[int, str, int]] = []
        for key, rel_name, outgoing in (("parent", "child_of", True), ("areas", "related", True),
                                        ("topics", "related", True), ("projects", "related", False)):
            for link in _links(fm.get(key)):
                target = self.target(link)
                tid = self.ids.get(target) if target in self.binder_dirs.values() else None
                if tid is None:
                    self.r.warn(doc.rel, f"{key} link [[{link}]] does not resolve to a binder")
                    continue
                wanted.append((oid, rel_name, tid) if outgoing else (tid, rel_name, oid))
        for src, rel_name, dst in wanted:
            if src != dst:
                self.conn.execute("insert into edge (src, rel, dst, source) values (%s, %s, %s, %s)"
                                  " on conflict do nothing", (src, rel_name, dst, BY))

    # -- notes

    def note_kind(self, doc: Doc) -> str:
        rel = PurePosixPath(doc.rel)
        if doc.type in NOTE_KINDS:
            return NOTE_KINDS[doc.type]
        if doc.type:
            return doc.type
        parts = rel.parts
        if parts[:3] == ("_system", "Routing", "Receipts"):
            return "receipt"
        if parts[:2] == ("_system", "Governance") or str(rel) == "_system/README.md":
            return "governance"
        if rel.name == "Sources.md":
            return "sources"
        if parts[:1] == ("Signals",):
            return "signal"
        if rel.name == "Trial Log.md":
            return "log"
        if rel.parent.name == "State":
            return "state"
        if "Incidents" in parts:
            return "incident"
        if "Decisions" in parts:
            return "decision"
        if parts[:1] == ("_system",):
            return "system"
        return "note"

    def import_note(self, doc: Doc) -> None:
        rel = doc.rel
        if rel in self.seen_notes:        # State/current.md, already folded into its binder
            return
        self.seen_notes.add(rel)
        kind = self.note_kind(doc)
        home = self.binder_of(rel)
        if kind == "receipt":
            mentioned = {t for t in (self.target(x) for x in _targets(doc.body))
                         if t in self.binder_dirs.values()}
            home = mentioned.pop() if len(mentioned) == 1 else None
        object_id = self.ids.get(home) if home else None
        attrs = _clean({k: _jsonable(v) for k, v in doc.fm.items() if k != "type"})
        title = _title(doc)
        if PurePosixPath(rel).parts[:1] == ("Signals",) and len(PurePosixPath(rel).parts) > 2:
            attrs["signal"] = signal = PurePosixPath(rel).parts[1]
            if signal.lower() not in title.lower():
                title = f"{signal}: {title}"            # "Teams: Authoritative sources"
        values = {"object_id": object_id, "kind": kind, "title": title, "body": _without_h1(doc.body),
                  "attrs": attrs}
        created = _when(doc.fm.get("created"))
        row = self.conn.execute("select * from note where origin ->> 'path' = %s", (rel,)).fetchone()
        if row:
            self.ids[rel] = row["id"]
            if row["origin"].get("hash") == doc.hash:
                self.r.count("note", "unchanged", kind)
                return
            if _fingerprint(self._note_values(row)) != row["origin"].get("fields_hash"):
                self.r.count("note", "kept", kind)
                self.r.kept_your_change.append(rel)
                return
            self.conn.execute(
                "update note set object_id = %(object_id)s, kind = %(kind)s, title = %(title)s, body = %(body)s,"
                " attrs = %(attrs)s, updated_at = now() where id = %(id)s",
                {**values, "attrs": Jsonb(attrs), "id": row["id"]})
            nid, outcome = row["id"], "updated"
        else:
            nid = self.conn.execute("insert into entity (kind) values ('note') returning id").fetchone()["id"]
            self.conn.execute(
                "insert into note (id, object_id, kind, title, body, attrs, created_at)"
                " values (%(id)s, %(object_id)s, %(kind)s, %(title)s, %(body)s, %(attrs)s, coalesce(%(created)s, now()))",
                {**values, "attrs": Jsonb(attrs), "id": nid, "created": created})
            self.ids[rel] = nid
            outcome = "created"
        new = self.conn.execute("select * from note where id = %s", (nid,)).fetchone()
        origin = _clean({"source": "obsidian", "path": rel, "uid": _text(doc.fm.get("uid")), "hash": doc.hash,
                         "fields_hash": _fingerprint(self._note_values(new)), "imported_at": self.now.isoformat()})
        self.conn.execute("update note set origin = %s where id = %s", (Jsonb(origin), nid))
        self.r.count("note", outcome, kind)

    @staticmethod
    def _note_values(row: dict) -> dict:
        return {k: row[k] for k in ("object_id", "kind", "title", "body", "attrs")}

    # -- work items

    def item_fields(self, doc: Doc) -> dict:
        rel, fm = doc.rel, doc.fm
        in_inbox = PurePosixPath(rel).parts[:2] == ("Inbox", "Items")
        status = str(fm.get("status") or "").strip().lower() or "inbox"
        if status in STATUS_MAP:
            self.r.status_mapped.append({"path": rel, "from": status, "to": STATUS_MAP[status]})
            status = STATUS_MAP[status]
        elif status not in work.STATUSES:
            self.r.warn(rel, f"unknown status {status!r}; imported as inbox")
            self.r.status_mapped.append({"path": rel, "from": status, "to": "inbox"})
            status = "inbox"
        home_links = _links(fm.get("home"))
        home_id, home_rel = None, None
        if home_links:
            home_rel = self.target(home_links[0])
            if home_rel in self.binder_dirs.values():
                home_id = self.ids.get(home_rel)
            if home_id is None:
                self.r.warn(rel, f"home [[{home_links[0]}]] does not resolve to an imported binder; no home")
            if len(home_links) > 1:
                self.r.warn(rel, "more than one home; the first was used")
        elif in_inbox:
            if status != "inbox":
                self.r.status_mapped.append({"path": rel, "from": status, "to": "inbox"})
                status = "inbox"
        else:
            self.r.warn(rel, "work item outside the Inbox has no home")
        folder_home = self.binder_of(rel)
        if home_rel and folder_home and home_rel != folder_home:
            self.r.warn(rel, f"home is {home_rel} but the file lives in {folder_home}; the home was used")
        dates = {}
        for key, col in (("due", "due"), ("review-after", "review_after")):
            d = _day(fm.get(key))
            if isinstance(d, str):
                self.r.warn(rel, f"{key} {d!r} is not a date; left empty")
                d = None
            dates[col] = d
        source = _clean({"kind": _text(fm.get("source-kind")), "account": _text(fm.get("source-account")),
                         "id": _text(fm.get("source-id")), "link": _text(fm.get("source-link"))})
        known = {"type", "uid", "status", "home", "related", "focus", "due", "review-after", "source-kind",
                 "source-account", "source-id", "source-link", "suggested-home", "inbox-reason", "routed-by",
                 "routed-at", "route-receipt", "created"}
        origin = _clean({
            "source": "obsidian", "path": rel, "routed_by": _text(fm.get("routed-by")),
            "routed_at": _text(fm.get("routed-at")), "route_receipt": _text(fm.get("route-receipt")),
            "suggested_home": _text(fm.get("suggested-home")), "inbox_reason": _text(fm.get("inbox-reason")),
            "created": _text(fm.get("created")), "home_link": home_links[0] if home_links and home_id is None else None,
            "frontmatter": _clean({k.replace("-", "_"): _jsonable(v) for k, v in fm.items() if k not in known}),
        })
        return {"title": _title(doc), "status": status, "home_id": home_id, "focus": _bool(fm.get("focus")),
                **dates, "body": _without_h1(doc.body), "source": source, "origin": origin,
                "created_at": _when(fm.get("created"))}

    def web_changed(self, row: dict) -> bool:
        if _fingerprint({k: row[k] for k in WORK_FIELDS}) != row["origin"].get("fields_hash"):
            return True
        return bool(self.conn.execute(
            "select 1 from work_item_event where work_item_id = %s and by <> %s and at > %s limit 1",
            (row["id"], BY, row["origin"].get("imported_at"))).fetchone())

    def import_item(self, doc: Doc) -> None:
        rel = doc.rel
        uid = _text(doc.fm.get("uid"))
        if not uid:
            uid = f"vault-path:{rel}"
            self.r.warn(rel, "work item has no uid; keyed on its path")
        if uid in self.seen_uids["work_item"]:
            self.r.warn(rel, f"duplicate uid {uid}; this copy was not imported")
            self.r.skip(rel, "duplicate uid")
            return
        self.seen_uids["work_item"].add(uid)
        f = self.item_fields(doc)
        row = self.conn.execute("select * from work_item where origin ->> 'uid' = %s", (uid,)).fetchone()
        if row:
            wid = self.ids[rel] = row["id"]
            if self.web_changed(row):
                if row["origin"].get("hash") != doc.hash:
                    self.r.count("work_item", "kept", f["status"])
                    self.r.kept_your_change.append(rel)
                else:
                    self.r.count("work_item", "unchanged", f["status"])
                self.report_links(doc, wid)
                return
            if row["origin"].get("hash") == doc.hash:
                self.r.count("work_item", "unchanged", f["status"])
                self.link(doc, wid)
                return
            work.update(self.conn, wid, by=BY, **{k: f[k] for k in WORK_FIELDS})
            self.conn.execute("update work_item set source = %s, created_at = coalesce(%s, created_at)"
                              " where id = %s", (Jsonb(f["source"]), f["created_at"], wid))
            outcome = "updated"
        else:
            wid = work.create(self.conn, f["title"], by=BY, status=f["status"], home_id=f["home_id"],
                              focus=f["focus"], due=f["due"], review_after=f["review_after"], body=f["body"],
                              source=f["source"], created_at=f["created_at"])
            self.ids[rel] = wid
            outcome = "created"
        new = self.conn.execute("select * from work_item where id = %s", (wid,)).fetchone()
        origin = {**f["origin"], "uid": uid, "hash": doc.hash,
                  "fields_hash": _fingerprint({k: new[k] for k in WORK_FIELDS}), "imported_at": self.now.isoformat()}
        self.conn.execute("update work_item set origin = %s where id = %s", (Jsonb(origin), wid))
        self.touched.add(rel)
        self.r.count("work_item", outcome, f["status"])
        self.link(doc, wid)

    def item_edges(self, doc: Doc) -> None:
        if doc.rel not in self.touched:
            return
        wid, fm = self.ids[doc.rel], doc.fm
        self.conn.execute("delete from edge where src = %s and source = %s and rel in ('related', 'receipt')",
                          (wid, BY))
        for link in _links(fm.get("related")):
            target = self.target(link)
            tid = self.ids.get(target) if target else None
            if tid is None or tid == wid:
                self.r.warn(doc.rel, f"related link [[{link}]] does not resolve to a binder or work item")
                continue
            self.conn.execute("insert into edge (src, rel, dst, source) values (%s, 'related', %s, %s)"
                              " on conflict do nothing", (wid, tid, BY))
        for link in _links(fm.get("route-receipt")):
            target = self.target(link)
            if target and target in self.ids and target not in self.binder_dirs.values():
                self.conn.execute("insert into edge (src, rel, dst, source) values (%s, 'receipt', %s, %s)"
                                  " on conflict do nothing", (wid, self.ids[target], BY))
            else:
                self.r.warn(doc.rel, f"route-receipt [[{link}]] does not resolve to an imported receipt")

    def link(self, doc: Doc, wid: int) -> None:
        ids, via, why = find_messages(self.conn, doc.fm)
        for mid in ids:
            work.link_message(self.conn, wid, mid, by=BY)
        self._report_link(doc, ids, via, why)

    def report_links(self, doc: Doc, wid: int) -> None:
        """For an item the owner changed: report its links as they are, add none."""
        ids = [r["dst"] for r in self.conn.execute(
            "select dst from edge where src = %s and rel = 'about' order by dst", (wid,)).fetchall()]
        if ids:
            self._report_link(doc, ids, "existing link", None)
        else:
            _, _, why = find_messages(self.conn, doc.fm)
            self._report_link(doc, [], None, why or ("not linked (changed in Talos Web; left alone)"
                                                     if doc.fm.get("source-link") else None))

    def _report_link(self, doc: Doc, ids: list[int], via: str | None, why: str | None) -> None:
        name = PurePosixPath(doc.rel).name
        if ids:
            self.r.links["linked"].append({"file": name, "path": doc.rel, "message_ids": ids, "via": via})
        elif why:
            self.r.links["not_found"].append({"file": name, "path": doc.rel,
                                              "kind": _text(doc.fm.get("source-kind")), "reason": why})
        else:
            self.r.links["no_link"].append(name)

    # -- the whole run

    def run(self) -> None:
        binders, items, notes = self.classify()
        for doc in sorted(binders, key=lambda d: d.rel):
            self.import_binder(doc)
        for doc in binders:
            self.binder_edges(doc)
        for doc in sorted(notes, key=lambda d: d.rel):
            self.import_note(doc)
        for doc in sorted(items, key=lambda d: d.rel):
            self.import_item(doc)
        for doc in items:
            self.item_edges(doc)
        self.missing()

    def missing(self) -> None:
        for what, table in (("binder", "object"), ("work item", "work_item")):
            seen = self.seen_uids["object" if table == "object" else "work_item"]
            for row in self.conn.execute(
                    f"select origin ->> 'uid' as uid, origin ->> 'path' as path from {table}"
                    " where origin ->> 'source' = 'obsidian' order by 2").fetchall():
                if row["uid"] not in seen:
                    self.r.missing_from_vault.append({"what": what, "path": row["path"], "uid": row["uid"]})
        for row in self.conn.execute("select origin ->> 'path' as path from note"
                                     " where origin ->> 'source' = 'obsidian' order by 1").fetchall():
            if row["path"] not in self.seen_notes and row["path"] not in self.r.placeholders:
                self.r.missing_from_vault.append({"what": "note", "path": row["path"]})


def import_vault(conn: psycopg.Connection, root: Path = DEFAULT_ROOT, *, dry_run: bool = False) -> dict:
    """Lift the vault at root into Talos Web. Returns the report (see format_report).

    Runs in one transaction; a dry run rolls it back and returns the same report. The
    vault is only read.
    """
    root = Path(root).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"no vault at {root}")
    now = datetime.now(UTC)
    report = Report(root=str(root), dry_run=dry_run, started_at=now.isoformat())
    with conn.transaction(force_rollback=dry_run):
        Importer(conn, root, report, now).run()
    return report.as_dict()
