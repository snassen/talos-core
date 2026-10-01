"""Write-back to Gmail over IMAP with Gmail's extensions (phase P3).

This is the only place Talos opens a Gmail folder read-write, and it does so only
for a committed changeset on an account with writeback_enabled (changesets.apply
checks both before an executor is ever called).

What each operation sends, one command per batch of UIDs:

    mark_read / mark_unread    STORE +FLAGS / -FLAGS (\\Seen)
    flag / unflag              STORE +FLAGS / -FLAGS (\\Flagged)
    add_label / remove_label   STORE +X-GM-LABELS / -X-GM-LABELS
    archive                    STORE -X-GM-LABELS (\\Inbox)
    move                       +X-GM-LABELS (destination), then -X-GM-LABELS (\\Inbox)
                               unless the destination is the inbox itself
    trash                      COPY to the \\Trash special folder (see _trash)
    move with from_trash       in the Trash folder: +X-GM-LABELS (\\Inbox) when the
                               destination is the inbox, then -X-GM-LABELS (\\Trash)

Safety, before any write:

- The target must be an All Mail location ('[all]') whose UIDVALIDITY equals the
  server's. If it does not, the target fails and nothing is guessed: plan again
  after the next sync.
- One FETCH of X-GM-MSGID confirms every UID still holds the message the mirror
  says it does. A UID that is gone or holds another message fails; IMAP would
  otherwise ignore a STORE to a missing UID silently and report success.
- Labels are refused unless they are \\Inbox, \\Trash, \\Starred or under Talos/.
  Talos writes labels only in its own namespace.

Nothing here expunges, sets \\Deleted, uses MOVE (which expunges the source) or
closes a folder (CLOSE expunges too). The furthest any operation goes is Trash.

A dry run (GmailExecutor(..., readonly=True).dry_run) makes the same checks with the
folders opened read-only (EXAMINE), reads the message's current flags and labels,
and lists the commands apply() would send. An executor built read-only refuses apply().
"""

from __future__ import annotations

import logging
from typing import Callable

from talos.changesets import Target
from talos.sources.base import chunks

log = logging.getLogger("talos.writeback.gmail")

FOLDER = "[all]"  # the mirror's name for Gmail's All Mail location
INBOX = "\\Inbox"
TRASH_LABEL = "\\Trash"
ALLOWED_SYSTEM_LABELS = {"\\Inbox", "\\Trash", "\\Starred"}
NAMESPACE = "Talos/"
BATCH = 1000  # UIDs per command; a longer UID set is split, still one command per slice
FLAG_OPS = {
    "mark_read": ("add_flags", b"\\Seen"),
    "mark_unread": ("remove_flags", b"\\Seen"),
    "flag": ("add_flags", b"\\Flagged"),
    "unflag": ("remove_flags", b"\\Flagged"),
}


class GmailWriteError(RuntimeError):
    pass


def label_allowed(label: str | None) -> bool:
    if not label:
        return False
    return label in ALLOWED_SYSTEM_LABELS or (label.startswith(NAMESPACE) and len(label) > len(NAMESPACE))


def _wire(label: str) -> str:
    """A label as IMAPClient puts it on the wire: UTF-7, quoted, backslashes escaped."""
    from imapclient.imap_utf7 import encode as encode_utf7
    from imapclient.imapclient import _quote
    return _quote(encode_utf7(label)).decode()


def _text(v) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)


def _key(d: dict, name: str):
    return d.get(name.encode()) if name.encode() in d else d.get(name)


class GmailExecutor:
    def __init__(self, address: str, password: Callable[[], str], *, host: str = "imap.gmail.com",
                 client_factory: Callable | None = None, readonly: bool = False):
        self.readonly = readonly
        self.address = address
        self.password = password
        self.host = host
        self.client_factory = client_factory
        self._client = None
        self._selected: str | None = None
        self._select_info: dict = {}
        self._folders: dict[bytes, str] = {}

    # -- connection ---------------------------------------------------------------

    def _connect(self):
        if self._client is None:
            if self.client_factory:
                self._client = self.client_factory()
            else:
                from imapclient import IMAPClient
                client = IMAPClient(self.host, ssl=True, timeout=120)
                client.login(self.address, self.password())
                self._client = client
        return self._client

    def _select(self, special_flag: bytes) -> tuple[object, str, dict]:
        """Select a special folder: read-write to apply, read-only (EXAMINE) for a dry run."""
        client = self._connect()
        folder = self._special(client, special_flag)
        if self._selected != folder:
            self._select_info = client.select_folder(folder, readonly=self.readonly)
            self._selected = folder
        return client, folder, self._select_info

    def _special(self, client, special_flag: bytes) -> str:
        if special_flag not in self._folders:
            folder = client.find_special_folder(special_flag)
            if not folder:
                raise GmailWriteError(f"Gmail shows no {special_flag.decode()} folder over IMAP")
            self._folders[special_flag] = folder
        return self._folders[special_flag]

    def close(self) -> None:
        """Log out. LOGOUT never expunges (unlike CLOSE, which this never sends)."""
        if self._client is not None:
            try:
                self._client.logout()
            except Exception:
                pass
        self._client = None
        self._selected = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- the Executor protocol ----------------------------------------------------

    def apply(self, op: str, args: dict, targets: list[Target]) -> dict[int, str | None]:
        results: dict[int, str | None] = {}
        unique: dict[int, Target] = {}
        for t in targets:
            unique.setdefault(t.message_id, t)
        targets = list(unique.values())
        if self.readonly:
            return {t.message_id: "refused: this executor was built for a dry run and writes nothing" for t in targets}

        refusal = self._refuse(op, args)
        if refusal:
            return {t.message_id: refusal for t in targets}
        try:
            if op == "move" and args.get("from_trash"):
                results.update(self._restore_from_trash(args.get("folder"), targets))
            else:
                results.update(self._in_all_mail(op, args, targets))
        except Exception as exc:
            log.warning("gmail write failed op=%s: %s", op, exc)
            err = f"{type(exc).__name__}: {exc}"
            for t in targets:
                results.setdefault(t.message_id, err)
        return results

    @staticmethod
    def _refuse(op: str, args: dict) -> str | None:
        if op in ("add_label", "remove_label"):
            label = args.get("label")
            if not label_allowed(label):
                return (f"refused: Talos only writes labels under {NAMESPACE} (or \\Inbox, \\Trash, \\Starred);"
                        f" {label!r} is not one")
        elif op == "move":
            dest = args.get("folder")
            if dest is None:
                return "refused: no destination was recorded for this move"
            if args.get("from_trash"):
                if dest not in (INBOX, FOLDER):
                    return f"refused: restoring from Trash goes to the inbox or All Mail, not {dest!r}"
            elif dest != INBOX and not (dest.startswith(NAMESPACE) and len(dest) > len(NAMESPACE)):
                return f"refused: Gmail moves go to \\Inbox or a label under {NAMESPACE}; {dest!r} is neither"
        elif op not in FLAG_OPS and op not in ("archive", "trash"):
            return f"refused: unknown operation {op!r}"
        return None

    # -- All Mail: flags, labels, archive, move, trash -----------------------------

    def _in_all_mail(self, op: str, args: dict, targets: list[Target]) -> dict[int, str | None]:
        from imapclient.imapclient import ALL
        results: dict[int, str | None] = {}
        client, _folder, info = self._select(ALL)
        server_validity = int(_key(info, "UIDVALIDITY"))
        usable: list[Target] = []
        for t in targets:
            if t.folder != FOLDER:
                results[t.message_id] = f"not an All Mail location ({t.folder!r}); Gmail writes go through All Mail"
            elif t.uid is None or t.uidvalidity is None:
                results[t.message_id] = "the mirror holds no UID for this message; sync first"
            elif int(t.uidvalidity) != server_validity:
                results[t.message_id] = (f"UIDVALIDITY changed (mirror {t.uidvalidity}, server {server_validity});"
                                         " nothing was written. Sync, then plan the changeset again")
            else:
                usable.append(t)

        for part in chunks(usable, BATCH):
            confirmed = self._confirm(client, part, results)
            if not confirmed:
                continue
            uids = [t.uid for t in confirmed]
            try:
                self._write(client, op, args, uids)
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                for t in confirmed:
                    results[t.message_id] = err
                continue
            for t in confirmed:
                results[t.message_id] = None
        return results

    @staticmethod
    def _confirm(client, targets: list[Target], results: dict) -> list[Target]:
        """Keep the targets whose UID still holds the expected message (one FETCH)."""
        data = client.fetch([t.uid for t in targets], ["X-GM-MSGID"])
        ok = []
        for t in targets:
            d = data.get(t.uid)
            if not d:
                results[t.message_id] = "no longer in All Mail at this UID; nothing was written"
                continue
            msgid = _key(d, "X-GM-MSGID")
            if t.provider_id is not None and str(msgid) != str(t.provider_id):
                results[t.message_id] = (f"UID {t.uid} holds another message (X-GM-MSGID {msgid},"
                                         f" expected {t.provider_id}); nothing was written")
                continue
            ok.append(t)
        return ok

    def _write(self, client, op: str, args: dict, uids: list[int]) -> None:
        if op in FLAG_OPS:
            method, flag = FLAG_OPS[op]
            getattr(client, method)(uids, [flag])
        elif op == "add_label":
            client.add_gmail_labels(uids, [args["label"]])
        elif op == "remove_label":
            client.remove_gmail_labels(uids, [args["label"]])
        elif op == "archive":
            client.remove_gmail_labels(uids, [INBOX])
        elif op == "move":
            dest = args["folder"]
            client.add_gmail_labels(uids, [dest])
            if dest != INBOX:
                client.remove_gmail_labels(uids, [INBOX])
        elif op == "trash":
            self._trash(client, uids)
        else:
            raise GmailWriteError(f"unknown operation {op!r}")

    def _trash(self, client, uids: list[int]) -> None:
        # Gmail's help says to delete from an IMAP client by moving the message to the
        # [Gmail]/Trash folder (found by its \Trash special-use flag, as its name is
        # localised). A classic IMAP "move" is COPY, then \Deleted, then EXPUNGE, and the
        # RFC 6851 MOVE command does the same in one step. Talos sends only the COPY:
        # Gmail treats a copy into Trash as moving the message to Trash (a trashed
        # message is no longer shown in All Mail). The \Deleted flag and the expunge are left out
        # on purpose: depending on the account's "When a message is marked as deleted
        # and expunged" setting, Gmail can delete forever on expunge, and Talos never
        # risks that. Gmail empties Trash after 30 days; the vault keeps the original.
        # Tested against a fake only: try it on one message before trusting it in bulk.
        from imapclient.imapclient import TRASH
        client.copy(uids, self._special(client, TRASH))

    # -- dry run: the same checks, read-only, nothing sent --------------------------

    def dry_run(self, op: str, args: dict, targets: list[Target]) -> dict[int, dict]:
        if not self.readonly:
            raise GmailWriteError("a dry run needs an executor built with readonly=True")
        unique: dict[int, Target] = {}
        for t in targets:
            unique.setdefault(t.message_id, t)
        targets = list(unique.values())
        refusal = self._refuse(op, args)
        if refusal:
            return {t.message_id: {"ok": False, "would": [], "problem": refusal} for t in targets}
        if op == "move" and args.get("from_trash"):
            return self._dry_restore(args.get("folder"), targets)
        from imapclient.imapclient import ALL
        client, folder, info = self._select(ALL)
        server_validity = int(_key(info, "UIDVALIDITY"))
        out: dict[int, dict] = {}
        usable: list[Target] = []
        for t in targets:
            problem = None
            if t.folder != FOLDER:
                problem = f"not an All Mail location ({t.folder!r}); Gmail writes go through All Mail"
            elif t.uid is None or t.uidvalidity is None:
                problem = "the mirror holds no UID for this message; sync first"
            elif int(t.uidvalidity) != server_validity:
                problem = f"UIDVALIDITY changed (mirror {t.uidvalidity}, server {server_validity}); sync and plan again"
            if problem:
                out[t.message_id] = {"ok": False, "would": [], "problem": problem}
            else:
                usable.append(t)
        if not usable:
            return out
        data = client.fetch([t.uid for t in usable], ["X-GM-MSGID", "FLAGS", "X-GM-LABELS"])
        for t in usable:
            d = data.get(t.uid)
            if not d:
                out[t.message_id] = {"ok": False, "would": [], "problem": f"no message at UID {t.uid} in All Mail"}
                continue
            msgid = _key(d, "X-GM-MSGID")
            server = {"folder": folder, "uid": t.uid, "x_gm_msgid": str(msgid),
                      "flags": sorted(_text(f) for f in (_key(d, "FLAGS") or ())),
                      "labels": sorted(_text(l) for l in (_key(d, "X-GM-LABELS") or ()))}
            if t.provider_id is not None and str(msgid) != str(t.provider_id):
                out[t.message_id] = {"ok": False, "would": [], "server": server,
                                     "problem": f"UID {t.uid} holds another message (X-GM-MSGID {msgid}, expected {t.provider_id})"}
                continue
            out[t.message_id] = {"ok": True, "would": self._commands(op, args, t.uid, folder), "server": server,
                                 "problem": None}
        return out

    def _commands(self, op: str, args: dict, uid: int, folder: str) -> list[str]:
        """The IMAP commands _write() sends for this operation, as text."""
        sel = f'SELECT "{folder}" (read-write)'
        if op in FLAG_OPS:
            method, flag = FLAG_OPS[op]
            return [sel, f"UID STORE {uid} {'+' if method == 'add_flags' else '-'}FLAGS ({flag.decode()})"]
        if op == "add_label":
            return [sel, f'UID STORE {uid} +X-GM-LABELS ({_wire(args["label"])})']
        if op == "remove_label":
            return [sel, f'UID STORE {uid} -X-GM-LABELS ({_wire(args["label"])})']
        if op == "archive":
            return [sel, f"UID STORE {uid} -X-GM-LABELS ({_wire(INBOX)})"]
        if op == "move":
            out = [sel, f'UID STORE {uid} +X-GM-LABELS ({_wire(args["folder"])})']
            if args["folder"] != INBOX:
                out.append(f"UID STORE {uid} -X-GM-LABELS ({_wire(INBOX)})")
            return out
        if op == "trash":
            from imapclient.imapclient import TRASH
            return [sel, f'UID COPY {uid} "{self._special(self._connect(), TRASH)}"   (a copy only; nothing is marked or removed)']
        return [f"(unknown operation {op!r})"]

    def _dry_restore(self, dest: str, targets: list[Target]) -> dict[int, dict]:
        from imapclient.imapclient import TRASH
        out: dict[int, dict] = {}
        client, folder, _info = self._select(TRASH)
        found = self._msgids_in(client)
        for t in targets:
            uid = found.get(str(t.provider_id)) if t.provider_id is not None else None
            if uid is None:
                out[t.message_id] = {"ok": False, "would": [], "problem": "not in Trash (emptied, or already restored)"}
                continue
            would = [f'SELECT "{folder}" (read-write)']
            if dest == INBOX:
                would.append(f"UID STORE {uid} +X-GM-LABELS ({_wire(INBOX)})")
            would.append(f"UID STORE {uid} -X-GM-LABELS ({_wire(TRASH_LABEL)})")
            out[t.message_id] = {"ok": True, "would": would, "server": {"folder": folder, "uid": uid}, "problem": None}
        return out

    # -- Trash: undoing a trash ----------------------------------------------------

    def _restore_from_trash(self, dest: str, targets: list[Target]) -> dict[int, str | None]:
        """Find the messages in Trash by X-GM-MSGID and take them out again.

        Trash has its own UIDs, so the mirror's All Mail UID is useless here; the
        permanent X-GM-MSGID is what identifies the message. Afterwards Trash is
        read again, and anything still there is reported as failed rather than
        assumed restored.
        """
        from imapclient.imapclient import TRASH
        results: dict[int, str | None] = {}
        want: dict[str, Target] = {}
        for t in targets:
            if t.provider_id is None:
                results[t.message_id] = "the mirror holds no X-GM-MSGID for this message"
            else:
                want[str(t.provider_id)] = t
        if not want:
            return results
        client, folder, _info = self._select(TRASH)
        found = self._msgids_in(client)
        uids = [uid for msgid, uid in found.items() if msgid in want]
        for msgid, t in want.items():
            if msgid not in found:
                results[t.message_id] = "not in Trash any more (emptied, or already restored)"
        if uids:
            for part in chunks(sorted(uids), BATCH):
                if dest == INBOX:
                    client.add_gmail_labels(part, [INBOX])
                client.remove_gmail_labels(part, [TRASH_LABEL])
            still = self._msgids_in(client)
            for msgid, t in want.items():
                if msgid in found:
                    results[t.message_id] = (
                        "still in Trash after the restore; restore it in Gmail by hand" if msgid in still else None)
        return results

    @staticmethod
    def _msgids_in(client) -> dict[str, int]:
        """X-GM-MSGID -> UID for the selected folder (one UID FETCH over all of it)."""
        client.noop()  # pick up changes made by our own STOREs before reading again
        uids = client.search(["ALL"])
        if not uids:
            return {}
        data = client.fetch(uids, ["X-GM-MSGID"])
        return {str(_key(d, "X-GM-MSGID")): uid for uid, d in data.items()}
