"""L3 executors: the only code that writes to a mail server.

changesets.apply() hands committed operations to one executor per account. This
package builds them, and only for accounts whose settings say
``writeback_enabled: true``. No account has that by default; switching it on is
the owner's decision, per account, once the read-only sync has proven itself.

Building an executor does not connect or read a secret: the Gmail executor logs in
on its first operation, and the Graph executor asks for a token only when it posts
a batch. So an apply that is refused (not committed, write-back off) never touches
the Keychain or the network.

Plain IMAP accounts (iCloud, Loopia) have no executor yet; apply() then refuses
with "no executor for account".
"""

from __future__ import annotations

import time
from typing import Callable

import httpx
import psycopg

from talos import secrets
from talos.changesets import Executor


def enabled_accounts(conn: psycopg.Connection) -> list[dict]:
    return conn.execute(
        "select id, provider, address, settings from account"
        " where coalesce((settings->>'writeback_enabled')::boolean, false) order by id").fetchall()


def graph_folders(conn: psycopg.Connection, account_id: str) -> dict[str, str]:
    """Readable folder path -> Graph folder id, from the sync cursors (scope 'folder:<id>')."""
    out = {}
    for r in conn.execute("select scope, state from sync_cursor where account_id = %s and scope like 'folder:%%'",
                          (account_id,)):
        state = r["state"] or {}
        folder_id = state.get("folder_id") or r["scope"].split(":", 1)[1]
        if state.get("path"):
            out[state["path"]] = folder_id
    return out


def executors_for(conn: psycopg.Connection, *, gmail_client_factory: Callable | None = None,
                  graph_http: httpx.Client | None = None, graph_token: Callable[[], str] | None = None,
                  sleep=time.sleep) -> dict[str, Executor]:
    """One executor per account with write-back switched on. The keyword arguments are for tests."""
    out: dict[str, Executor] = {}
    for acct in enabled_accounts(conn):
        s = acct["settings"]
        if acct["provider"] == "gmail":
            from talos.writeback.gmail import GmailExecutor
            secret = s.get("secret")
            out[acct["id"]] = GmailExecutor(acct["address"], lambda secret=secret: secrets.get(secret),
                                            client_factory=gmail_client_factory)
        elif acct["provider"] == "graph":
            from talos.writeback.graph import GraphExecutor
            token = graph_token or _graph_token(acct["id"], s)
            out[acct["id"]] = GraphExecutor(token, graph_http, folders=graph_folders(conn, acct["id"]),
                                            archive_folder=s.get("archive_folder"), sleep=sleep)
    return out


def dry_runners_for(conn: psycopg.Connection, *, gmail_client_factory: Callable | None = None,
                    graph_http: httpx.Client | None = None, graph_token: Callable[[], str] | None = None) -> dict:
    """Read-only executors for dry runs, for every Gmail account whether or not write-back is on.

    Gmail's open folders with EXAMINE; Microsoft 365's only read (GET, in JSON batches) with the
    read-only sign-in. Both refuse apply(), so a dry run can be made before write-back is ever
    switched on.
    """
    from talos.writeback.gmail import GmailExecutor
    from talos.writeback.graph import GraphExecutor
    out = {}
    for acct in conn.execute("select id, provider, address, settings from account where provider in ('gmail', 'graph')"):
        s = acct["settings"]
        if acct["provider"] == "gmail":
            secret = s.get("secret")
            out[acct["id"]] = GmailExecutor(acct["address"], lambda secret=secret: secrets.get(secret),
                                            client_factory=gmail_client_factory, readonly=True)
        elif graph_token is not None or s.get("tenant_id"):
            out[acct["id"]] = GraphExecutor(graph_token or _graph_read_token(acct["id"], s), graph_http,
                                            folders=graph_folders(conn, acct["id"]),
                                            archive_folder=s.get("archive_folder"), readonly=True)
    return out


def _graph_token(account_id: str, settings: dict) -> Callable[[], str]:
    """A token getter that signs in lazily, so building executors reads no secret."""
    auth = None

    def token() -> str:
        # The import stays outside the "first call" branch: inside it, WRITE_SCOPES was a local
        # that only the first call set, and the second batch of a changeset failed.
        from talos.graphauth import WRITE_SCOPES, GraphAuth
        nonlocal auth
        if auth is None:
            auth = GraphAuth(account_id, settings["tenant_id"], settings["client_id"])
        return auth.token(scopes=WRITE_SCOPES)

    return token


def _graph_read_token(account_id: str, settings: dict) -> Callable[[], str]:
    """The read-only token (graphauth.SCOPES), signed in lazily: a dry run never holds a write token."""
    auth = None

    def token() -> str:
        from talos.graphauth import GraphAuth
        nonlocal auth
        if auth is None:
            auth = GraphAuth(account_id, settings["tenant_id"], settings["client_id"])
        return auth.token()

    return token


def close_all(executors: dict[str, Executor]) -> None:
    for ex in executors.values():
        close = getattr(ex, "close", None)
        if close:
            close()
