"""Write-back executors (phase P3) against fake Gmail and Graph servers.

The fakes keep enough server state to show an operation and its undo really cancel
out, and they fail loudly on anything Talos must never send: EXPUNGE, MOVE, CLOSE,
\\Deleted, HTTP DELETE, sending mail.
"""

import json
import logging

import httpx
import mailfactory as mf
import pytest
from psycopg.types.json import Jsonb

from talos import changesets
from talos.changesets import Target
from talos.ingest import Location
from talos.writeback import dry_runners_for, executors_for
from talos.writeback.gmail import GmailExecutor
from talos.writeback.graph import GraphExecutor, GraphWriteError

ALL_MAIL = "[Gmail]/Alla mail"
TRASH = "[Gmail]/Papperskorgen"
WRITES = {"add_flags", "remove_flags", "add_gmail_labels", "remove_gmail_labels", "copy"}


class FakeGmailWrite:
    """IMAPClient as Gmail answers it, with labels as state. Anything that expunges raises."""

    def __init__(self, uidvalidity=1):
        self.uidvalidity = uidvalidity
        self.folders = {ALL_MAIL: {}, TRASH: {}}  # folder -> uid -> {"msgid", "labels", "flags"}
        self.next_uid = {ALL_MAIL: 100, TRASH: 500}
        self.selected = None
        self.selects = []
        self.commands = []  # (name, folder, uids, argument) for every write

    def add(self, uid, msgid, labels=("\\Inbox",), flags=()):
        self.folders[ALL_MAIL][uid] = {"msgid": msgid, "labels": set(labels), "flags": set(flags)}

    def by_msgid(self, folder):
        return {m["msgid"]: m for m in self.folders[folder].values()}

    # reads
    def find_special_folder(self, flag):
        return {b"\\All": ALL_MAIL, b"\\Trash": TRASH}[flag]

    def select_folder(self, name, readonly=False):
        self.selected = name
        self.selects.append((name, readonly))
        return {b"UIDVALIDITY": self.uidvalidity if name == ALL_MAIL else 99,
                b"EXISTS": len(self.folders[name]), b"READ-WRITE": not readonly}

    def fetch(self, uids, items):
        box = self.folders[self.selected]
        if list(items) == ["X-GM-MSGID", "FLAGS", "X-GM-LABELS"]:  # the dry run's look at the current state
            assert self.selects[-1][1] is True, "a dry run reads through a read-only select"
            return {u: {b"X-GM-MSGID": box[u]["msgid"], b"FLAGS": tuple(f.encode() for f in box[u]["flags"]),
                        b"X-GM-LABELS": tuple(l.encode() for l in box[u]["labels"])} for u in uids if u in box}
        assert list(items) == ["X-GM-MSGID"], "the executor only ever reads message ids"
        return {u: {b"X-GM-MSGID": box[u]["msgid"]} for u in uids if u in box}

    def search(self, criteria):
        assert criteria == ["ALL"]
        return sorted(self.folders[self.selected])

    def noop(self):
        pass

    def logout(self):
        pass

    # writes
    def _write(self, name, uids, arg):
        assert self.selects[-1][1] is False, "writes need a read-write select"
        self.commands.append((name, self.selected, tuple(uids), tuple(arg) if not isinstance(arg, str) else arg))
        return [self.folders[self.selected][u] for u in uids if u in self.folders[self.selected]]

    def add_flags(self, uids, flags):
        for m in self._write("add_flags", uids, flags):
            m["flags"] |= set(flags)

    def remove_flags(self, uids, flags):
        for m in self._write("remove_flags", uids, flags):
            m["flags"] -= set(flags)

    def add_gmail_labels(self, uids, labels):
        for m in self._write("add_gmail_labels", uids, labels):
            m["labels"] |= set(labels)

    def remove_gmail_labels(self, uids, labels):
        box = self.folders[self.selected]
        for m in self._write("remove_gmail_labels", uids, labels):
            m["labels"] -= set(labels)
        if self.selected == TRASH and "\\Trash" in labels:  # out of Trash: back in All Mail
            for u in list(uids):
                m = box.pop(u)
                self.folders[ALL_MAIL][self.next_uid[ALL_MAIL]] = m
                self.next_uid[ALL_MAIL] += 1

    def copy(self, uids, folder):
        assert folder == TRASH, "the only copy Talos makes is into Trash"
        box = self.folders[self.selected]
        self._write("copy", uids, folder)
        for u in uids:  # Gmail: a copy into Trash moves the message there
            self.folders[TRASH][self.next_uid[TRASH]] = box.pop(u)
            self.next_uid[TRASH] += 1

    def __getattr__(self, name):  # expunge, uid_expunge, move, close_folder, delete_messages, set_flags …
        raise AssertionError(f"Talos called IMAP {name}(); write-back must never do that")


class FakeGraphWrite:
    """Graph's JSON batch endpoint over a few messages. Refuses anything but GET, PATCH and move."""

    def __init__(self, ids=("AAA", "BBB"), folder="F1"):
        self.messages = {i: {"folder": folder, "isRead": False, "flag": "notFlagged", "categories": []} for i in ids}
        self.batches = []
        self.throttle = {}  # message id -> how many times to answer 429 first
        self.throttle_batches = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/v1.0/$batch", f"{request.method} {request.url}"
        assert request.headers["Authorization"] == "Bearer token"
        reqs = json.loads(request.content)["requests"]
        assert 0 < len(reqs) <= 20, "Graph takes at most 20 requests per batch"
        self.batches.append(reqs)
        if self.throttle_batches:
            self.throttle_batches -= 1
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"responses": [{"id": r["id"], **self.handle(r)} for r in reqs]})

    def handle(self, r):
        assert r["method"] in ("GET", "PATCH", "POST"), f"Talos sent {r['method']} to Graph"
        for bad in ("/send", "sendMail", "permanentDelete", "/reply", "/forward", "createReply"):
            assert bad not in r["url"], f"Talos asked Graph for {bad}"
        assert 'IdType="ImmutableId"' in r["headers"]["Prefer"]
        parts = r["url"].split("?")[0].strip("/").split("/")
        if parts[:2] == ["me", "mailFolders"] and r["method"] == "GET":  # a well-known folder's real id
            return {"status": 200, "body": {"id": parts[2], "displayName": parts[2]}}
        assert parts[:2] == ["me", "messages"]
        mid = parts[2]
        if self.throttle.get(mid):
            self.throttle[mid] -= 1
            return {"status": 429, "headers": {"Retry-After": "3"},
                    "body": {"error": {"code": "TooManyRequests", "message": "slow down"}}}
        m = self.messages.get(mid)
        if m is None:
            return {"status": 404, "body": {"error": {"code": "ErrorItemNotFound", "message": "not found"}}}
        if r["method"] == "GET":
            return {"status": 200, "body": {"id": mid, "categories": list(m["categories"]), "parentFolderId": m["folder"],
                                            "subject": f"Subject {mid}", "isRead": m["isRead"]}}
        assert r["headers"]["Content-Type"] == "application/json"
        body = r["body"]
        if r["method"] == "PATCH":
            assert set(body) <= {"isRead", "flag", "categories"}
            if "flag" in body:
                m["flag"] = body["flag"]["flagStatus"]
            m.update({k: v for k, v in body.items() if k != "flag"})
            return {"status": 200, "body": {"id": mid}}
        assert parts[3:] == ["move"] and set(body) == {"destinationId"}
        m["folder"] = body["destinationId"]
        return {"status": 201, "body": {"id": mid}}


def graph_executor(fake, slept=None, **kw):
    return GraphExecutor(lambda: "token", httpx.Client(transport=httpx.MockTransport(fake)),
                         sleep=(slept if slept is not None else []).append, **kw)


def targets(n, folder="[all]", uidvalidity=1, first_uid=1):
    return [Target(i, folder, uidvalidity, first_uid + i - 1, str(1000 + i)) for i in range(1, n + 1)]


def enable_writeback(conn, account, **settings):
    conn.execute("update account set settings = settings || %s where id = %s",
                 (Jsonb({"writeback_enabled": True, **settings}), account))


def seed_gmail(conn, ingestor, fake, mails):
    """mails: key -> (uid, msgid, labels). Mirror and fake server agree to begin with."""
    ids = {}
    for key, (uid, msgid, labels) in mails.items():
        fake.add(uid, msgid, labels=labels)
        ids[key] = ingestor.ingest("gmail", mf.make(subject=key), Location(
            "[all]", str(msgid), uidvalidity=fake.uidvalidity, uid=uid, provider_id=str(msgid),
            labels=list(labels))).message_id
    conn.commit()
    return ids


def run_changeset(conn, cs, executors):
    changesets.plan(conn, cs)
    changesets.commit(conn, cs, max_ops=1000)
    return changesets.apply(conn, cs, executors)


# -- Gmail ------------------------------------------------------------------------


def test_gmail_sends_one_imap_command_per_batch_for_flags_and_labels():
    fake = FakeGmailWrite()
    for i in range(1, 6):
        fake.add(i, 1000 + i)
    ex = GmailExecutor("x", lambda: "pw", client_factory=lambda: fake)
    assert set(ex.apply("mark_read", {}, targets(5)).values()) == {None}
    assert set(ex.apply("add_label", {"label": "Talos/Kvitto"}, targets(5)).values()) == {None}
    assert set(ex.apply("archive", {}, targets(5)).values()) == {None}
    assert fake.commands == [
        ("add_flags", ALL_MAIL, (1, 2, 3, 4, 5), (b"\\Seen",)),
        ("add_gmail_labels", ALL_MAIL, (1, 2, 3, 4, 5), ("Talos/Kvitto",)),
        ("remove_gmail_labels", ALL_MAIL, (1, 2, 3, 4, 5), ("\\Inbox",)),
    ]
    assert fake.selects == [(ALL_MAIL, False)]  # opened read-write once, inside the executor


def test_gmail_move_to_a_talos_label_is_one_command_per_label_change():
    fake = FakeGmailWrite()
    for i in range(1, 4):
        fake.add(i, 1000 + i)
    ex = GmailExecutor("x", lambda: "pw", client_factory=lambda: fake)
    assert set(ex.apply("move", {"folder": "Talos/Projekt"}, targets(3)).values()) == {None}
    assert [(c[0], c[2], c[3]) for c in fake.commands] == [
        ("add_gmail_labels", (1, 2, 3), ("Talos/Projekt",)), ("remove_gmail_labels", (1, 2, 3), ("\\Inbox",))]


def test_gmail_a_changed_uidvalidity_fails_safely_and_writes_nothing():
    fake = FakeGmailWrite(uidvalidity=2)
    fake.add(1, 1001)
    ex = GmailExecutor("x", lambda: "pw", client_factory=lambda: fake)
    result = ex.apply("mark_read", {}, targets(1, uidvalidity=1))
    assert "UIDVALIDITY changed (mirror 1, server 2)" in result[1]
    assert fake.commands == []


def test_gmail_a_uid_that_no_longer_holds_the_message_is_not_written():
    fake = FakeGmailWrite()
    fake.add(1, 1001)
    fake.add(2, 9999)  # the mirror thinks UID 2 is message 1002
    ex = GmailExecutor("x", lambda: "pw", client_factory=lambda: fake)
    result = ex.apply("flag", {}, targets(3))  # UID 3 is gone altogether
    assert result[1] is None
    assert "holds another message" in result[2] and "no longer in All Mail" in result[3]
    assert fake.commands == [("add_flags", ALL_MAIL, (1,), (b"\\Flagged",))]


@pytest.mark.parametrize("label", ["Receipts", "School", "\\Important", "Talos/", "", None, "talos/x"])
def test_gmail_refuses_labels_outside_the_talos_namespace_without_connecting(label):
    connected = []
    ex = GmailExecutor("x", lambda: "pw", client_factory=lambda: connected.append(1) or FakeGmailWrite())
    for op in ("add_label", "remove_label"):
        result = ex.apply(op, {"label": label}, targets(2))
        assert all(err and err.startswith("refused: Talos only writes labels under Talos/") for err in result.values())
    assert ex.apply("move", {"folder": "Receipts"}, targets(1))[1].startswith("refused")
    assert connected == []


def test_gmail_allows_the_system_labels_it_needs():
    fake = FakeGmailWrite()
    fake.add(1, 1001, labels=())
    ex = GmailExecutor("x", lambda: "pw", client_factory=lambda: fake)
    for label in ("\\Inbox", "\\Starred", "Talos/Kund/Telia"):
        assert ex.apply("add_label", {"label": label}, targets(1)) == {1: None}


def test_gmail_archive_then_undo_through_changesets_restores_the_inbox(conn, ingestor):
    fake = FakeGmailWrite()
    ids = seed_gmail(conn, ingestor, fake, {"kvitto": (1, 111, ("\\Inbox", "Receipts")),
                                            "skola": (2, 222, ("\\Inbox",)),
                                            "backup": (3, 333, ("Notifications",))})
    enable_writeback(conn, "gmail")
    executors = executors_for(conn, gmail_client_factory=lambda: fake)
    cs = changesets.create(conn, "Arkivera", "archive", message_ids=list(ids.values()))
    assert run_changeset(conn, cs, executors) == {"done": 2, "failed": 0}  # backup was never in the inbox
    assert {m: sorted(r["labels"]) for m, r in fake.by_msgid(ALL_MAIL).items()} == {
        111: ["Receipts"], 222: [], 333: ["Notifications"]}

    undo = changesets.undo(conn, cs)
    changesets.commit(conn, undo, max_ops=1000)
    assert changesets.apply(conn, undo, executors) == {"done": 2, "failed": 0}
    assert {m: sorted(r["labels"]) for m, r in fake.by_msgid(ALL_MAIL).items()} == {
        111: ["Receipts", "\\Inbox"], 222: ["\\Inbox"], 333: ["Notifications"]}
    assert [(c[0], c[2]) for c in fake.commands] == [("remove_gmail_labels", (1, 2)), ("add_gmail_labels", (1, 2))]


def test_gmail_trash_copies_into_trash_and_undo_takes_it_out_again_without_any_expunge(conn, ingestor):
    fake = FakeGmailWrite()
    ids = seed_gmail(conn, ingestor, fake, {"nyhetsbrev": (1, 111, ("\\Inbox",)),
                                            "arkiverad": (2, 222, ("Talos/Kund",))})
    enable_writeback(conn, "gmail")
    executors = executors_for(conn, gmail_client_factory=lambda: fake)
    cs = changesets.create(conn, "Papperskorg", "trash", message_ids=list(ids.values()))
    assert run_changeset(conn, cs, executors) == {"done": 2, "failed": 0}
    assert fake.folders[ALL_MAIL] == {} and set(fake.by_msgid(TRASH)) == {111, 222}
    assert fake.commands == [("copy", ALL_MAIL, (1, 2), TRASH)]

    undo = changesets.undo(conn, cs)
    changesets.commit(conn, undo, max_ops=1000)
    assert changesets.apply(conn, undo, executors) == {"done": 2, "failed": 0}
    assert fake.folders[TRASH] == {}
    back = fake.by_msgid(ALL_MAIL)
    assert back[111]["labels"] == {"\\Inbox"}  # it was in the inbox, so it goes back there
    assert back[222]["labels"] == {"Talos/Kund"}  # it was archived, so it only leaves Trash
    for name, _folder, _uids, arg in fake.commands:
        assert name in WRITES and "expunge" not in name
        assert b"\\Deleted" not in (arg if isinstance(arg, tuple) else ())


def test_gmail_a_restore_reports_mail_that_is_no_longer_in_trash():
    fake = FakeGmailWrite()
    ex = GmailExecutor("x", lambda: "pw", client_factory=lambda: fake)
    result = ex.apply("move", {"folder": "\\Inbox", "from_trash": True}, targets(1))
    assert result == {1: "not in Trash any more (emptied, or already restored)"}
    assert fake.commands == []


# -- Graph ------------------------------------------------------------------------


def test_graph_sends_at_most_20_requests_per_batch_and_reports_each_message():
    ids = [f"M{i:02}" for i in range(45)]
    fake = FakeGraphWrite(ids=ids)
    ex = graph_executor(fake)
    ts = [Target(i, "Inkorgen", None, None, mid) for i, mid in enumerate(ids, 1)]
    ts.append(Target(99, "Inkorgen", None, None, "GONE"))
    result = ex.apply("mark_read", {}, ts)
    assert [len(b) for b in fake.batches] == [20, 20, 6]
    assert all(result[i] is None for i in range(1, 46))
    assert result[99].startswith("Graph 404: ErrorItemNotFound")
    assert all(m["isRead"] for m in fake.messages.values())


def test_graph_retries_throttled_requests_after_retry_after():
    fake = FakeGraphWrite()
    fake.throttle = {"BBB": 1}
    fake.throttle_batches = 1
    slept = []
    ex = graph_executor(fake, slept)
    result = ex.apply("flag", {}, [Target(1, "Inkorgen", None, None, "AAA"), Target(2, "Inkorgen", None, None, "BBB")])
    assert result == {1: None, 2: None}
    assert slept == [7.0, 3.0]  # the whole batch once, then only BBB
    assert [[r["id"] for r in b] for b in fake.batches] == [["1", "2"], ["1", "2"], ["2"]]
    assert {m["flag"] for m in fake.messages.values()} == {"flagged"}


def test_graph_gives_up_on_a_message_that_stays_throttled():
    fake = FakeGraphWrite()
    fake.throttle = {"AAA": 100}
    ex = graph_executor(fake, max_retries=3)
    result = ex.apply("mark_read", {}, [Target(1, "Inkorgen", None, None, "AAA")])
    assert "still throttled after 3 attempts" in result[1]


def test_graph_categories_are_read_modified_written_and_only_talos_ones_are_added():
    fake = FakeGraphWrite()
    fake.messages["AAA"]["categories"] = ["Röd kategori"]
    ex = graph_executor(fake)
    ts = [Target(1, "Inkorgen", None, None, "AAA"), Target(2, "Inkorgen", None, None, "BBB")]
    assert ex.apply("add_label", {"label": "Talos/Kund"}, ts) == {1: None, 2: None}
    assert fake.messages["AAA"]["categories"] == ["Röd kategori", "Talos/Kund"]  # the user's own is kept
    assert ex.apply("remove_label", {"label": "Talos/Kund"}, ts[:1]) == {1: None}
    assert fake.messages["AAA"]["categories"] == ["Röd kategori"]
    n = len(fake.batches)
    refused = ex.apply("add_label", {"label": "Röd kategori"}, ts)
    assert all(e.startswith("refused") for e in refused.values()) and len(fake.batches) == n


def test_graph_a_move_to_an_unknown_folder_is_refused_not_guessed():
    fake = FakeGraphWrite()
    ex = graph_executor(fake, folders={"Inkorgen": "F1"})
    result = ex.apply("move", {"folder": "Någon annan mapp"}, [Target(1, "Arkiv", None, None, "AAA")])
    assert result[1].startswith("refused: cannot resolve folder")
    assert fake.batches == []


def seed_graph(conn, ingestor, keys):
    ids = {}
    for key in keys:
        ids[key] = ingestor.ingest("work", mf.make(subject=key), Location(  # as the Graph sync records it
            "F1", key, provider_id=key, folder_path="Inkorgen")).message_id
    for fid, path in (("F1", "Inkorgen"), ("F9", "Arkiv")):
        conn.execute("insert into sync_cursor (account_id, scope, state) values ('work', %s, %s)",
                     (f"folder:{fid}", Jsonb({"path": path, "folder_id": fid, "delta_link": "x"})))
    conn.commit()
    return ids


def test_graph_trash_then_undo_through_changesets_moves_it_back_to_its_folder(conn, ingestor):
    ids = seed_graph(conn, ingestor, ["AAA", "BBB"])
    enable_writeback(conn, "work")
    fake = FakeGraphWrite()
    executors = executors_for(conn, graph_http=httpx.Client(transport=httpx.MockTransport(fake)),
                              graph_token=lambda: "token", sleep=lambda s: None)
    cs = changesets.create(conn, "Papperskorg", "trash", message_ids=[ids["AAA"]])
    assert run_changeset(conn, cs, executors) == {"done": 1, "failed": 0}
    assert fake.messages["AAA"]["folder"] == "deleteditems" and fake.messages["BBB"]["folder"] == "F1"

    undo = changesets.undo(conn, cs)
    changesets.commit(conn, undo, max_ops=1000)
    assert changesets.apply(conn, undo, executors) == {"done": 1, "failed": 0}
    assert fake.messages["AAA"]["folder"] == "F1"  # "Inkorgen" resolved through the cursor


def test_graph_archive_goes_to_the_configured_archive_folder_and_back(conn, ingestor):
    ids = seed_graph(conn, ingestor, ["AAA"])
    enable_writeback(conn, "work", archive_folder="Arkiv")
    fake = FakeGraphWrite(ids=["AAA"])
    executors = executors_for(conn, graph_http=httpx.Client(transport=httpx.MockTransport(fake)),
                              graph_token=lambda: "token")
    cs = changesets.create(conn, "Arkivera", "archive", message_ids=[ids["AAA"]])
    assert run_changeset(conn, cs, executors) == {"done": 1, "failed": 0}
    assert fake.messages["AAA"]["folder"] == "F9"
    undo = changesets.undo(conn, cs)
    changesets.commit(conn, undo, max_ops=1000)
    changesets.apply(conn, undo, executors)
    assert fake.messages["AAA"]["folder"] == "F1"


def test_graph_planning_reads_folders_by_their_path_when_locations_are_keyed_by_folder_id(conn, ingestor):
    ids = seed_graph(conn, ingestor, ["AAA"])
    enable_writeback(conn, "work", archive_folder="Inkorgen")
    for op, args, reason in (("move", {"folder": "Inkorgen"}, "already there"),
                             ("archive", {}, "already archived")):
        cs = changesets.create(conn, op, op, args=args, message_ids=[ids["AAA"]])
        assert changesets.plan(conn, cs)["skipped_because"] == {reason: 1}


def test_graph_without_an_archive_folder_uses_the_well_known_archive():
    fake = FakeGraphWrite()
    graph_executor(fake).apply("archive", {}, [Target(1, "Inkorgen", None, None, "AAA")])
    assert fake.messages["AAA"]["folder"] == "archive"


# -- switched off -----------------------------------------------------------------


def test_no_executor_is_built_for_an_account_without_writeback_enabled(conn):
    assert executors_for(conn) == {}
    conn.execute("update account set settings = settings || %s where id = 'work'",
                 (Jsonb({"writeback_enabled": False}),))
    assert executors_for(conn) == {}
    enable_writeback(conn, "gmail")
    built = executors_for(conn, gmail_client_factory=lambda: pytest.fail("building must not connect"))
    assert list(built) == ["gmail"] and isinstance(built["gmail"], GmailExecutor)


def test_the_accounts_seed_leaves_writeback_off():
    from talos.accounts import ACCOUNTS
    assert not any(a["settings"].get("writeback_enabled") for a in ACCOUNTS)


def test_cli_apply_refuses_clearly_when_writeback_is_off(conn, ingestor, database, tmp_path, monkeypatch, capsys):
    from talos import cli

    fake = FakeGmailWrite()
    ids = seed_gmail(conn, ingestor, fake, {"kvitto": (1, 111, ("\\Inbox",))})
    cs = changesets.create(conn, "Läst", "mark_read", message_ids=list(ids.values()))
    changesets.plan(conn, cs)
    changesets.commit(conn, cs, max_ops=1000)
    conn.commit()
    monkeypatch.setenv("TALOS_DSN", database)
    monkeypatch.setenv("TALOS_HOME", str(tmp_path))
    root = logging.getLogger()
    handlers = list(root.handlers)
    try:
        with pytest.raises(SystemExit) as exit_info:
            cli.main(["changeset", "apply", str(cs)])
    finally:
        for h in root.handlers[len(handlers):]:
            h.close()
        root.handlers = handlers
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "refused: write-back is not enabled for account 'gmail'" in err
    assert "nothing was sent to a server" in err
    row = conn.execute("select status from changeset where id = %s", (cs,)).fetchone()
    assert row["status"] == "committed"  # refused before anything started; it can run once enabled
    assert conn.execute("select count(*) n from changeset_op where changeset_id = %s and status = 'pending'",
                        (cs,)).fetchone()["n"] == 1


# -- guard rails before the first real test (docs/writeback-test-plan.md) ------------


def test_commit_refuses_more_messages_than_its_limit(conn, ingestor):
    fake = FakeGmailWrite()
    ids = seed_gmail(conn, ingestor, fake, {"a": (1, 1001, ["\\Inbox"]), "b": (2, 1002, ["\\Inbox"])})
    cs = changesets.create(conn, "t", "archive", message_ids=list(ids.values()))
    changesets.plan(conn, cs)
    with pytest.raises(changesets.TooLarge, match="would change 2 messages; the limit for this commit is 1"):
        changesets.commit(conn, cs)
    assert conn.execute("select status from changeset where id = %s", (cs,)).fetchone()["status"] == "planned"
    changesets.commit(conn, cs, max_ops=2)
    assert conn.execute("select status from changeset where id = %s", (cs,)).fetchone()["status"] == "committed"


def test_a_single_message_commits_under_the_default_limit(conn, ingestor):
    fake = FakeGmailWrite()
    ids = seed_gmail(conn, ingestor, fake, {"a": (1, 1001, ["\\Inbox"])})
    cs = changesets.create(conn, "t", "flag", message_ids=[ids["a"]])
    changesets.plan(conn, cs)
    changesets.commit(conn, cs)


def test_dry_run_reads_only_and_reports_the_commands(conn, ingestor):
    fake = FakeGmailWrite()
    ids = seed_gmail(conn, ingestor, fake, {"test": (7, 4242, ["\\Inbox"])})
    cs = changesets.create(conn, "t", "add_label", args={"label": "Talos/test"}, message_ids=[ids["test"]])
    changesets.plan(conn, cs)
    runner = GmailExecutor("x", lambda: "pw", client_factory=lambda: fake, readonly=True)
    report = changesets.dry_run(conn, cs, {"gmail": runner})
    assert fake.commands == [] and all(ro for _name, ro in fake.selects), "a dry run writes nothing"
    assert report["ok"] == 1 and report["problems"] == 0 and report["sent_nothing"]
    item = report["items"][0]
    assert item["server"]["labels"] == ["\\Inbox"] and item["server"]["uid"] == 7
    assert item["would"] == [f'SELECT "{ALL_MAIL}" (read-write)', 'UID STORE 7 +X-GM-LABELS ("Talos/test")']
    # the plan is untouched: still pending, still planned, and the report is kept for the UI
    assert conn.execute("select status from changeset_op where changeset_id = %s", (cs,)).fetchone()["status"] == "pending"
    row = conn.execute("select status, summary from changeset where id = %s", (cs,)).fetchone()
    assert row["status"] == "planned" and row["summary"]["dry_run"]["ok"] == 1


def test_dry_run_catches_a_uid_that_holds_another_message(conn, ingestor):
    fake = FakeGmailWrite()
    ids = seed_gmail(conn, ingestor, fake, {"test": (7, 4242, ["\\Inbox"])})
    fake.folders[ALL_MAIL][7]["msgid"] = 9999  # the server moved on since the last sync
    cs = changesets.create(conn, "t", "flag", message_ids=[ids["test"]])
    changesets.plan(conn, cs)
    report = changesets.dry_run(conn, cs, {"gmail": GmailExecutor("x", lambda: "pw", client_factory=lambda: fake,
                                                                 readonly=True)})
    assert report["problems"] == 1 and "holds another message" in report["items"][0]["problem"]


def test_dry_run_of_a_trash_undo_finds_the_message_in_trash(conn, ingestor):
    fake = FakeGmailWrite()
    ids = seed_gmail(conn, ingestor, fake, {"test": (7, 4242, ["\\Inbox"])})
    enable_writeback(conn, "gmail")
    cs = changesets.create(conn, "t", "trash", message_ids=[ids["test"]])
    run_changeset(conn, cs, {"gmail": GmailExecutor("x", lambda: "pw", client_factory=lambda: fake)})
    undo = changesets.undo(conn, cs)
    fake.commands.clear()
    report = changesets.dry_run(conn, undo, {"gmail": GmailExecutor("x", lambda: "pw", client_factory=lambda: fake,
                                                                   readonly=True)})
    assert fake.commands == [] and report["ok"] == 1
    assert report["items"][0]["would"][-1] == 'UID STORE 500 -X-GM-LABELS ("\\\\Trash")'


def test_a_read_only_executor_refuses_to_apply():
    fake = FakeGmailWrite()
    fake.add(1, 1001)
    ex = GmailExecutor("x", lambda: "pw", client_factory=lambda: fake, readonly=True)
    assert "dry run" in ex.apply("flag", {}, targets(1))[1]
    assert fake.commands == [] and fake.selects == []


def test_dry_run_needs_no_writeback_but_apply_still_does(conn, ingestor):
    fake = FakeGmailWrite()
    ids = seed_gmail(conn, ingestor, fake, {"test": (7, 4242, ["\\Inbox"])})
    cs = changesets.create(conn, "t", "flag", message_ids=[ids["test"]])
    changesets.plan(conn, cs)
    from talos.writeback import dry_runners_for
    runners = dry_runners_for(conn, gmail_client_factory=lambda: fake)
    assert changesets.dry_run(conn, cs, runners)["ok"] == 1
    changesets.commit(conn, cs)
    with pytest.raises(changesets.WritebackDisabled):
        changesets.apply(conn, cs, executors_for(conn, gmail_client_factory=lambda: fake))
    assert fake.commands == []


def test_a_trash_undo_still_restores_after_a_sync_has_marked_the_mail_gone(conn, ingestor):
    # The real order of events: trash, the 5-minute sync sees the mail leave All Mail, then undo.
    fake = FakeGmailWrite()
    ids = seed_gmail(conn, ingestor, fake, {"test": (7, 4242, ["\\Inbox"]), "other": (8, 4343, ["\\Inbox"])})
    enable_writeback(conn, "gmail")
    cs = changesets.create(conn, "t", "trash", message_ids=[ids["test"]])
    ex = GmailExecutor("x", lambda: "pw", client_factory=lambda: fake)
    assert run_changeset(conn, cs, {"gmail": ex}) == {"done": 1, "failed": 0}
    ingestor.mark_gone("gmail", "[all]", uids=[7])  # what the sync does next
    conn.commit()
    undo = changesets.undo(conn, cs)
    report = changesets.dry_run(conn, undo, {"gmail": GmailExecutor("x", lambda: "pw", client_factory=lambda: fake,
                                                                   readonly=True)})
    assert report["ok"] == 1, report
    changesets.commit(conn, undo)
    assert changesets.apply(conn, undo, {"gmail": GmailExecutor("x", lambda: "pw", client_factory=lambda: fake)}) \
        == {"done": 1, "failed": 0}
    assert fake.by_msgid(ALL_MAIL)[4242]["labels"] >= {"\\Inbox"} and 4242 not in fake.by_msgid(TRASH)
    # an ordinary operation on a gone message is still skipped, never guessed
    cs2 = changesets.create(conn, "t2", "flag", message_ids=[ids["other"]])
    changesets.plan(conn, cs2)
    ingestor.mark_gone("gmail", "[all]", uids=[8])
    conn.commit()
    changesets.commit(conn, cs2)
    assert changesets.apply(conn, cs2, {"gmail": ex}) == {"done": 0, "failed": 0}
    assert conn.execute("select status from changeset_op where changeset_id = %s", (cs2,)).fetchone()["status"] == "skipped"


def test_graph_dry_run_reads_each_message_says_what_it_would_send_and_sends_nothing(conn, ingestor):
    ids = seed_graph(conn, ingestor, ["AAA", "BBB"])
    fake = FakeGraphWrite()
    fake.messages["BBB"]["folder"] = "deleteditems"
    runners = dry_runners_for(conn, graph_http=httpx.Client(transport=httpx.MockTransport(fake)), graph_token=lambda: "token")
    cs = changesets.create(conn, "Papperskorg", "trash", message_ids=[ids["AAA"], ids["BBB"]])
    changesets.plan(conn, cs)
    report = changesets.dry_run(conn, cs, runners)
    by = {i["message_id"]: i for i in report["items"]}
    assert by[ids["AAA"]]["ok"] and by[ids["AAA"]]["server"]["folder"] == "Inkorgen"
    assert by[ids["AAA"]]["would"][0].startswith('POST /me/messages/AAA/move {"destinationId": "deleteditems"}')
    assert (by[ids["BBB"]]["ok"], by[ids["BBB"]]["problem"]) == (False, "the mirror has it elsewhere (F1); sync first")
    assert all(r["method"] == "GET" for b in fake.batches for r in b)
    assert (fake.messages["AAA"]["folder"], report["sent_nothing"]) == ("F1", True)


def test_a_read_only_graph_executor_refuses_to_apply():
    ex = graph_executor(FakeGraphWrite(), readonly=True)
    with pytest.raises(GraphWriteError, match="read-only"):
        ex.apply("trash", {}, targets(1))


def test_apply_records_its_timing_and_the_check_finds_the_messages_on_the_server(conn, ingestor):
    ids = seed_graph(conn, ingestor, ["AAA", "BBB"])
    enable_writeback(conn, "work")
    fake = FakeGraphWrite()
    http = httpx.Client(transport=httpx.MockTransport(fake))
    executors = executors_for(conn, graph_http=http, graph_token=lambda: "token", sleep=lambda s: None)
    cs = changesets.create(conn, "Papperskorg", "trash", message_ids=[ids["AAA"], ids["BBB"]])
    changesets.plan(conn, cs)
    changesets.commit(conn, cs, max_ops=2)
    assert changesets.apply(conn, cs, executors) == {"done": 2, "failed": 0}
    timing = conn.execute("select summary->'apply' as t from changeset where id = %s", (cs,)).fetchone()["t"]
    assert timing["done"] == 2 and timing["connections"]["work"]["batches"] == 1 and timing["connections"]["work"]["requests"] == 2
    check = changesets.check(conn, cs, dry_runners_for(conn, graph_http=http, graph_token=lambda: "token"))
    assert (check["checked"], check["server"], check["problems"]) == (2, {"deleteditems": 2}, {})
    assert check["mirror"] == {"Inkorgen": 2} and check["mirror_caught_up"] == 0  # the mirror follows at the next sync
    assert changesets.check(conn, cs, dry_runners_for(conn, graph_http=http, graph_token=lambda: "token"), sample=1)["checked"] == 1


def test_the_write_token_getter_gives_a_token_for_every_batch_not_only_the_first(monkeypatch):
    import talos.graphauth
    from talos.writeback import _graph_token

    class FakeAuth:
        made = 0

        def __init__(self, *a):
            FakeAuth.made += 1

        def token(self, scopes=None):
            return "write-token:" + ",".join(scopes)

    monkeypatch.setattr(talos.graphauth, "GraphAuth", FakeAuth)
    token = _graph_token("work", {"tenant_id": "t", "client_id": "c"})
    assert token() == token() == "write-token:Mail.ReadWrite"  # the second call failed before 28 Sep 2026
    assert FakeAuth.made == 1


def test_a_batch_that_breaks_keeps_the_results_of_the_batches_before_it():
    fake = FakeGraphWrite(ids=[str(1000 + i) for i in range(1, 26)])
    calls = []

    def breaking(request):
        calls.append(1)
        if len(calls) == 2:
            raise httpx.ConnectError("the line went down")
        return fake(request)

    results = graph_executor(breaking).apply("trash", {}, targets(25))
    moved = [mid for mid, err in results.items() if err is None]
    assert len(moved) == 20 and all(fake.messages[str(1000 + m)]["folder"] == "deleteditems" for m in moved)
    assert all("the line went down" in results[m] for m in range(21, 26))


def test_reconcile_marks_a_failed_move_done_when_the_server_already_has_it_there(conn, ingestor):
    ids = seed_graph(conn, ingestor, ["AAA", "BBB"])
    enable_writeback(conn, "work")
    fake = FakeGraphWrite()
    http = httpx.Client(transport=httpx.MockTransport(fake))
    cs = changesets.create(conn, "Papperskorg", "trash", message_ids=[ids["AAA"], ids["BBB"]])
    changesets.plan(conn, cs)
    changesets.commit(conn, cs, max_ops=2)
    conn.execute("update changeset_op set status = 'failed', error = 'Graph 0: broke' where changeset_id = %s", (cs,))
    conn.execute("update changeset set status = 'failed' where id = %s", (cs,))
    fake.messages["AAA"]["folder"] = "deleteditems"  # the server carried out AAA before the line broke
    runners = dry_runners_for(conn, graph_http=http, graph_token=lambda: "token")
    assert changesets.reconcile(conn, cs, runners) == {"confirmed_done": 1, "still_failed": 1}
    assert changesets.retry(conn, cs) == 1
    executors = executors_for(conn, graph_http=http, graph_token=lambda: "token", sleep=lambda s: None)
    assert changesets.apply(conn, cs, executors) == {"done": 1, "failed": 0}
    assert fake.messages["BBB"]["folder"] == "deleteditems"
    assert conn.execute("select status from changeset where id = %s", (cs,)).fetchone()["status"] == "done"
    undo = changesets.undo(conn, cs)  # both, the confirmed one too
    assert conn.execute("select count(*) as n from changeset_op where changeset_id = %s", (undo,)).fetchone()["n"] == 2
