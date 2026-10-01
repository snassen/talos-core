"""The owner's accounts, and how to build a sync source for each.

IDs and hosts only. No secrets: each account names the Keychain item that holds its password, or,
for Microsoft, the Entra app (client and tenant IDs are identifiers, not credentials). The list and
every address that is "me" live in TALOS_HOME/config/accounts.json (talos.personal), outside the
repository; rules/accounts.example.json shows the shape. Teams chats and channels come through the
work account's Microsoft sign-in (settings.token_account) as an account of their own, so its sync
runs, cursors and lock are its own; it is seeded disabled, since switching it on is the owner's call.
"""

from __future__ import annotations

import json

import psycopg
from psycopg.types.json import Jsonb

from talos import personal, secrets


def _load() -> tuple[list[dict], dict[str, str | None]]:
    """The owner's accounts and addresses (TALOS_HOME/config/accounts.json), else the repository's example."""
    path = personal.find("accounts.json", personal.EXAMPLES / "accounts.example.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["accounts"], data["my_addresses"]


# The accounts, and every address that is "me": direction (in, out, self) is decided against it.
ACCOUNTS, MY_ADDRESSES = _load()


def ui() -> list[dict]:
    """Each account as the web shows it, in order: its name, letter, colour (a series 1 to 7, or "other")
    and ink. An account's "ui" in accounts.json sets any of them (and "order"); else they are its display
    name, its first letter, and a colour by its place in the list."""
    out = []
    for i, a in enumerate(ACCOUNTS):
        u = a.get("ui") or {}
        name = u.get("name") or a.get("display_name") or a["id"]
        out.append({"id": a["id"], "name": name, "letter": u.get("letter") or name[:1].upper(),
                    "color": u.get("color") or i % 7 + 1, "ink": u.get("ink"), "order": u.get("order", i + 1)})
    return [{k: v for k, v in x.items() if k != "order"} for x in sorted(out, key=lambda x: x["order"])]


def seed(conn: psycopg.Connection) -> None:
    """Insert missing accounts and addresses. Existing settings are merged, never replaced."""
    for a in ACCOUNTS:
        conn.execute(
            "insert into account (id, provider, address, display_name, enabled, settings)"
            " values (%s, %s, %s, %s, %s, %s)"
            " on conflict (id) do update set settings = excluded.settings || account.settings",
            (a["id"], a["provider"], a["address"], a["display_name"], a["enabled"], Jsonb(a["settings"])))
    for address, account in MY_ADDRESSES.items():
        conn.execute("insert into my_address (address, account_id) values (%s, %s) on conflict do nothing",
                     (address, account))


def source_for(account: dict, *, recent: int | None = None, only_channel: dict | None = None):
    """The sync adapter for one account row. recent: Teams's fast lane (the N most recent chats only)."""
    s = account["settings"]
    provider = account["provider"]
    if provider == "gmail":
        from talos.sources.gmail import GmailSource
        return GmailSource(account["address"], lambda: secrets.get(s["secret"]))
    if provider == "graph":
        from talos.graphauth import GraphAuth
        from talos.sources.graph import GraphClient, GraphMailSource
        auth = GraphAuth(account["id"], s["tenant_id"], s["client_id"])
        return GraphMailSource(GraphClient(auth.token, refresh=lambda: auth.token(force_refresh=True)),
                               skip_folders=set(s.get("skip_folders", [])),
                               include_hidden=bool(s.get("include_hidden_folders", False)))
    if provider == "teams":
        from talos.graphauth import GraphAuth
        from talos.sources.graph import GraphClient
        from talos.sources.teams import TeamsSource
        auth = GraphAuth(s["token_account"], s["tenant_id"], s["client_id"])   # the Microsoft 365 account it signs in as
        return TeamsSource(GraphClient(auth.token, refresh=lambda: auth.token(force_refresh=True)),
                           channels=s.get("channels", True), extra_channels=s.get("extra_channels"), recent=recent,
                           only_channel=only_channel)
    if provider == "imap":
        from talos.sources.imap import ImapSource
        return ImapSource(account["address"], lambda: secrets.get(s["secret"]), s["host"],
                          username=s.get("username"), port=s.get("port", 993))
    raise ValueError(f"account {account['id']} ({provider}) has no network source")
