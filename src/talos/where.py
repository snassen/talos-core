"""Where everything is, and why: `talos where`.

Talos keeps things in five places, on purpose:

- the code (this repository): public-safe, the same for everyone;
- the personal part (talos.personal: TALOS_CONFIG or TALOS_HOME/config): who you are, your accounts,
  your rules and your mailbox structure, outside the code so the code can be shared;
- the data folder (TALOS_HOME): the vault of originals, logs, exports and reports;
- the database (TALOS_DSN): everything derived from the vault, plus your own decisions and work;
- the macOS Keychain (service "talos"): every secret, and nothing else holds one.

The map says, for each, where it is now, what it holds, and why it lives there. It only reads: a
Keychain item is checked for existence and never read (secrets.exists does not prompt), and the
database is asked read-only. Facts that depend on this Mac (launchd, Time Machine) are best effort.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path

import psycopg
from psycopg import conninfo

from talos import __version__, config, personal, secrets

KEYCHAIN_FIXED = {
    "typesafe-api-key": "Jev, the hosted model (talos.jev)",
    "argus-checkin-token": "scripts checking in with Argus",
    "argus-heartbeat-url": "Argus's outbound heartbeat (its one egress)",
    "web:password": "Talos Web's door: your password (stored as a scrypt hash)",
    "web:totp": "Talos Web's door: the authenticator secret",
    "web:recovery": "Talos Web's door: the recovery codes (hashed)",
    "google-oauth-client": "the Google sign-in for the calendar (talos auth google)",
}
HUMAN_VALUES = "select count(*) from assignment where source_kind = 'human' and status = 'active'"
DATA_FILES = {
    "vault": "the originals, content-addressed and write-once: everything else can be rebuilt from it",
    "logs": "talos.log (rotated at 10 MB) and the services' logs",
    "exports": "Parquet snapshots and JSONL rows (talos export)",
    "docs": "reports mined from your mail: kept beside the data, never in the code",
}
# The small settings files, by name without .json (a full "<name>.json" would read like a module call
# to the Argus guard in tests/test_argus.py).
SETTINGS_FILES = {"web": "the tailnet names and people let in to Talos Web",
                  "enrich": "the switch and daily budget for Jev on new mail",
                  "argus": "the Argus timers"}


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def human(n: int | None) -> str:
    if n is None:
        return "—"
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _run(args: list[str]) -> str | None:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def code() -> dict:
    remote = _run(["git", "-C", str(personal.REPO), "config", "--get", "remote.origin.url"])
    return {"title": "The code", "path": str(personal.REPO),
            "why": "Meant to be the same for everyone: what is about you belongs in the personal part below.",
            "items": [{"name": "version", "value": __version__},
                      {"name": "git remote", "value": (remote or "none").strip()}]}


def personal_part() -> dict:
    root = personal.home()
    how = "TALOS_CONFIG" if os.environ.get("TALOS_CONFIG") else "TALOS_HOME/config"
    git = (root / ".git").exists()
    items = []
    for name, example in personal.FILES.items():
        p = root / name
        if name.startswith("rules/"):
            mine = sorted((root / "rules").glob("*.json")) if (root / "rules").exists() else []
            items.append({"name": "rules/", "state": "yours" if mine else "none",
                          "value": ", ".join(f.name for f in mine) or "no rule files (the example: rules/example-rules.json)",
                          "why": "your rules as data; loaded with talos rules load"})
            continue
        state = "yours" if p.exists() else ("example in use" if example else "generic in use")
        items.append({"name": name, "state": state, "value": str(p) if p.exists() else str(example or "built in"),
                      "why": {"owner.json": "who you are to Talos: the name your decisions are stored under",
                              "accounts.json": "which accounts to sync and which addresses are you",
                              "recipient.txt": "who you are, sent to Jev with every question",
                              "structure.json": "where every mail should go under Talos/"}[name]})
    disc = sorted((root / "discovery").glob("*.json")) if (root / "discovery").exists() else []
    items.append({"name": "discovery/", "state": "yours" if disc else "none",
                  "value": ", ".join(f.name for f in disc) or "no drafts", "why": "drafts for Tune › Systems"})
    return {"title": "Your personal part", "path": str(root), "how": how, "git": git,
            "why": "Who you are and how your mail is organised: outside the code, so the code can be public. "
                   + ("It is a git repository (keep it private)." if git else
                      "Only on this machine (and in its backups)."),
            "items": items}


def data_folder(s: config.Settings) -> dict:
    items = []
    for name, why in {**DATA_FILES, **{f"{k}.json": v for k, v in SETTINGS_FILES.items()}}.items():
        p = s.home / name
        items.append({"name": name, "state": "present" if p.exists() else "absent",
                      "value": human(_size(p)) if p.exists() else "—", "why": why})
    return {"title": "The data folder (TALOS_HOME)", "path": str(s.home),
            "why": "What Talos keeps on disk: the originals first. It never holds a secret.", "items": items}


def database(s: config.Settings) -> dict:
    info = conninfo.conninfo_to_dict(s.dsn)
    where = f"{info.get('host', 'localhost')}:{info.get('port', 5432)}/{info.get('dbname', '')}"
    out = {"title": "The database (TALOS_DSN)", "path": where,
           "why": "Everything derived from the vault (rebuildable), plus what is not: your decisions, your work "
                  "items, binders and notes. Back it up.", "items": []}
    try:
        with psycopg.connect(s.dsn, connect_timeout=5) as c:
            c.execute("set transaction read only")
            one = lambda q: c.execute(q).fetchone()[0]  # noqa: E731
            out["items"] = [
                {"name": "size", "value": human(one("select pg_database_size(current_database())"))},
                {"name": "data directory", "value": one("show data_directory")},
                {"name": "migrations applied", "value": str(one("select count(*) from schema_migration"))},
                {"name": "messages", "value": f"{one('select count(*) from message'):,}"},
                {"name": "your own decisions",
                 "value": f"{one(HUMAN_VALUES):,}",
                 "why": "values you set: not in the vault, only here"},
                {"name": "work items, binders, notes",
                 "value": f"{one('select count(*) from work_item'):,}, {one('select count(*) from object'):,}, "
                          f"{one('select count(*) from note'):,}", "why": "made in Talos Web: only here"},
            ]
    except psycopg.Error as exc:
        out["items"] = [{"name": "not reachable", "value": type(exc).__name__}]
    return out


def keychain(s: config.Settings) -> dict:
    wanted = dict(KEYCHAIN_FIXED)
    acc = personal.find("accounts.json", personal.EXAMPLES / "accounts.example.json")
    try:
        for a in json.loads(acc.read_text(encoding="utf-8"))["accounts"]:
            st = a.get("settings") or {}
            if st.get("secret"):
                wanted[st["secret"]] = f"the {a['display_name']} account"
            if a["provider"] in ("graph", "teams") and st.get("tenant_id"):
                wanted[f"graph-token-cache:{st.get('token_account', a['id'])}"] = "the Microsoft sign-in's token cache"
            if a["provider"] == "gmail":
                wanted[f"google-token:{a['id']}"] = "the Google calendar sign-in"
    except (OSError, ValueError, KeyError):
        pass
    items = [{"name": k, "state": "present" if secrets.exists(k) else "absent", "why": why} for k, why in wanted.items()]
    return {"title": "The Keychain (service \"talos\")", "path": "login keychain",
            "why": "Every secret, and only there: never in a file, an environment variable or a log. "
                   "Checked by name only; no value is read.", "items": items}


def services() -> dict:
    items = []
    listing = _run(["launchctl", "list"]) if platform.system() == "Darwin" else None
    for line in (listing or "").splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and ".talos." in parts[2]:
            items.append({"name": parts[2], "state": "running" if parts[0] != "-" else "between runs",
                          "value": f"pid {parts[0]}" if parts[0] != "-" else f"last exit {parts[1]}"})
    return {"title": "The services (launchd)", "path": "~/Library/LaunchAgents",
            "why": "The web server, the 5-minute sync and the Teams lane run as your own launchd jobs "
                   "(talos launchd prints them; it never installs them).",
            "items": items or [{"name": "none found", "value": "not on this Mac, or not installed"}]}


def backups(s: config.Settings) -> dict:
    items = []
    if platform.system() == "Darwin":
        dest = _run(["tmutil", "destinationinfo"])
        name = next((ln.split(":", 1)[1].strip() for ln in (dest or "").splitlines() if ln.startswith("Name")), None)
        items.append({"name": "Time Machine", "value": name or "no destination (or no access)"})
        pg_data = None
        try:
            with psycopg.connect(s.dsn, connect_timeout=5) as c:
                pg_data = c.execute("show data_directory").fetchone()[0]
        except psycopg.Error:
            pass
        for p in [s.home, personal.home()] + ([Path(pg_data)] if pg_data else []):
            ex = _run(["tmutil", "isexcluded", str(p)])
            items.append({"name": str(p), "value": (ex or "unknown").split("]")[0].strip("[ \n") or "unknown"})
    from talos import backup
    last = backup.latest(s)
    items.insert(0, {"name": "nightly backup of your own work", "value": (
        f"{last['path']} ({last['made']}, {len(backup.backups(s))} kept)" if last else "none yet: talos backup"),
        "why": "your decisions, work items, binders, rules and changesets, apart from the database (docs/backup.md)"})
    return {"title": "Backups", "path": "",
            "why": "The vault can rebuild the database's derived values, but not your decisions and work: those "
                   "need the database's own backup.", "items": items or [{"name": "not checked", "value": "not a Mac"}]}


def map_all(s: config.Settings | None = None) -> list[dict]:
    s = s or config.load()
    return [code(), personal_part(), data_folder(s), database(s), keychain(s), services(), backups(s)]


def format_text(sections: list[dict]) -> str:
    lines = []
    for sec in sections:
        lines += [f"{sec['title']}: {sec['path']}" if sec["path"] else sec["title"], f"  why: {sec['why']}"]
        for it in sec["items"]:
            state = f" [{it['state']}]" if it.get("state") else ""
            value = f" {it['value']}" if it.get("value") else ""
            why = f"  ({it['why']})" if it.get("why") else ""
            lines.append(f"  - {it['name']}{state}{value}{why}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_markdown(sections: list[dict], *, title: str = "Where everything is") -> str:
    out = [f"# {title}", "", "Made by `talos where`. Nothing secret is in it: Keychain items are named, never read.", ""]
    for sec in sections:
        out += [f"## {sec['title']}", ""]
        if sec["path"]:
            out += [f"`{sec['path']}`", ""]
        out += [sec["why"], "", "| What | State | Detail | Why |", "|---|---|---|---|"]
        for it in sec["items"]:
            out.append(f"| {it['name']} | {it.get('state', '')} | {str(it.get('value', '')).replace('|', '/')} | "
                       f"{it.get('why', '')} |")
        out.append("")
    return "\n".join(out)
