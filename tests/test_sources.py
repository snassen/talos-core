"""Sync adapters against fake servers. The fakes also check the adapters never write."""

from datetime import datetime, timezone

import httpx
import mailfactory as mf

from talos.sources import base
from talos.sources.gmail import GmailSource
from talos.sources.graph import GraphClient, GraphMailSource
from talos.sources.local import LocalSource

T0 = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)


class FakeGmail:
    """Just enough of IMAPClient, as Gmail answers it. Fails loudly on anything that writes."""

    def __init__(self):
        self.uidvalidity = 7
        self.modseq = 100
        self.messages = {}  # uid -> dict of fetch items
        self.changed = {}
        self.calls = []
        self.searches = []
        self.left_out_of_batches = set()  # UIDs a multi-UID FETCH omits (a single-UID FETCH returns them)
        self.never_returned = set()  # UIDs every FETCH omits
        self.fail_on = None  # a UID whose body FETCH breaks the connection

    def add(self, uid, raw, *, msgid, thrid, labels=(b"\\Inbox",), flags=()):
        self.messages[uid] = {b"X-GM-MSGID": msgid, b"X-GM-THRID": thrid, b"X-GM-LABELS": labels,
                              b"FLAGS": flags, b"INTERNALDATE": T0, b"BODY[]": raw,
                              b"RFC822.SIZE": len(raw) if isinstance(raw, bytes) else 0}

    def find_special_folder(self, flag):
        return "[Gmail]/Alla mail"

    def select_folder(self, name, readonly=False):
        assert readonly, "Talos must select folders read-only"
        return {b"UIDVALIDITY": self.uidvalidity, b"HIGHESTMODSEQ": self.modseq}

    def search(self, criteria):
        self.searches.append(list(criteria))
        if criteria == ["ALL"]:
            return list(self.messages)
        if criteria[0] == "MODSEQ":
            if len(self.changed) > self.__dict__.get("max_modseq_answer", 10**9):
                import imaplib
                raise imaplib.IMAP4.abort("command: SEARCH => System Error")  # as Gmail does
            return sorted(self.changed)
        if len(criteria) == 4 and criteria[0] == "UID" and criteria[2] == "MODSEQ":
            lo, hi = (int(x) for x in criteria[1].split(":"))
            return sorted(u for u in self.changed if lo <= u <= hi)
        assert criteria[0] == "UID", criteria
        low = int(criteria[1].split(":")[0])
        uids = sorted(self.messages)
        # As a real server: 'N:*' matches the highest UID even when it is below N.
        return sorted({u for u in uids if u >= low} | ({uids[-1]} if uids else set()))

    def has_capability(self, name):
        return name == "CONDSTORE"

    def fetch(self, uids, items, modifiers=None):
        self.calls.append((uids, tuple(items), modifiers))
        assert "BODY[]" not in items, "bodies must be fetched with BODY.PEEK[] so \\Seen is not set"
        # As IMAPClient 4 does: the answer is filtered through int() of each requested id,
        # so a range such as "1:*" raises. The fake must not accept what the real client refuses.
        [int(u) for u in (uids if isinstance(uids, (list, tuple)) else [uids])]
        if modifiers:
            return self.changed
        if set(items) == {"X-GM-MSGID", "X-GM-LABELS", "FLAGS"}:
            return {u: self.changed[u] for u in uids if u in self.changed}
        if "BODY.PEEK[]" in items and self.fail_on in uids:
            raise ConnectionResetError("connection dropped")
        out = {}
        for u in uids:
            if u not in self.messages or u in self.never_returned:
                continue
            if len(uids) > 1 and u in self.left_out_of_batches and "BODY.PEEK[]" in items:
                continue
            out[u] = self.messages[u]
        return out

    def logout(self):
        pass

    def __getattr__(self, name):  # store, copy, move, expunge, append, delete_messages …
        raise AssertionError(f"Talos called IMAP {name}(); sync must never write")


def gmail_sync(conn, ingestor, fake, **kw):
    return base.run(conn, ingestor, "gmail", GmailSource("x", lambda: "pw", client_factory=lambda: fake), **kw)


def test_gmail_first_sync_reads_everything_and_the_next_only_what_is_new(conn, ingestor):
    fake = FakeGmail()
    fake.add(1, mf.make(subject="Ett"), msgid=111, thrid=900)
    fake.add(2, mf.make(subject="Två", frm="owner@gmail.com", to="a@b.se"), msgid=222, thrid=900,
             labels=(b"\\Sent",))
    s1 = gmail_sync(conn, ingestor, fake)
    assert s1.added == 2
    fake.add(5, mf.make(subject="Tre"), msgid=333, thrid=901)
    s2 = gmail_sync(conn, ingestor, fake)
    assert s2.added == 1
    assert conn.execute("select state from sync_cursor where account_id='gmail'").fetchone()["state"]["last_uid"] == 5
    rows = {r["subject"]: r for r in conn.execute("select subject, direction, thread_id from message")}
    assert rows["Två"]["direction"] == "out"
    assert rows["Ett"]["thread_id"] == rows["Två"]["thread_id"] != rows["Tre"]["thread_id"]
    assert conn.execute("select status from sync_run order by id desc limit 1").fetchone()["status"] == "ok"


def test_gmail_backfill_in_slices_resumes_where_it_stopped(conn, ingestor):
    fake = FakeGmail()
    for uid in range(1, 8):
        fake.add(uid, mf.make(subject=f"m{uid}"), msgid=1000 + uid, thrid=uid)
    assert gmail_sync(conn, ingestor, fake, limit=3).added == 3
    assert gmail_sync(conn, ingestor, fake, limit=3).added == 3
    assert gmail_sync(conn, ingestor, fake).added == 1
    assert conn.execute("select count(*) n from message").fetchone()["n"] == 7


def test_gmail_flag_and_label_changes_on_old_mail_are_mirrored(conn, ingestor):
    fake = FakeGmail()
    fake.add(1, mf.make(), msgid=111, thrid=1)
    gmail_sync(conn, ingestor, fake)
    gmail_sync(conn, ingestor, fake)  # records highestmodseq
    fake.modseq = 150
    fake.changed = {1: {b"X-GM-MSGID": 111, b"X-GM-LABELS": (b"Receipts",), b"FLAGS": (b"\\Seen", b"\\Flagged")}}
    gmail_sync(conn, ingestor, fake)
    loc = conn.execute("select flags, labels from message_location").fetchone()
    assert loc["flags"] == ["flagged", "seen"] and loc["labels"] == ["Receipts"]
    assert ["MODSEQ", "100"] in fake.searches


def test_gmail_mail_that_leaves_all_mail_is_marked_gone_but_kept(conn, ingestor):
    fake = FakeGmail()
    fake.add(1, mf.make(subject="kvar"), msgid=111, thrid=1)
    fake.add(2, mf.make(subject="borta"), msgid=222, thrid=2)
    gmail_sync(conn, ingestor, fake)
    del fake.messages[2]
    assert gmail_sync(conn, ingestor, fake).gone == 1
    present = {r["subject"]: r["present"] for r in conn.execute(
        "select m.subject, l.present from message m join message_location l on l.message_id = m.id")}
    assert present == {"kvar": True, "borta": False}


def test_gmail_a_new_uidvalidity_does_not_duplicate_anything(conn, ingestor):
    fake = FakeGmail()
    fake.add(1, mf.make(), msgid=111, thrid=1)
    gmail_sync(conn, ingestor, fake)
    fake.uidvalidity = 8
    fake.messages = {40: fake.messages[1]}
    stats = gmail_sync(conn, ingestor, fake)
    assert stats.added == 0 and stats.updated == 1
    assert conn.execute("select count(*) n from message").fetchone()["n"] == 1


class FakeGraph:
    def __init__(self):
        self.raw = {"A": mf.make(subject="Hej från Graph"), "B": mf.make(subject="Andra"),
                    "C": mf.make(subject="I undermappen")}
        self.round = 1
        self.throttle_once = True
        self.requests = []
        self.top = [{"id": "F1", "displayName": "Inkorgen", "childFolderCount": 1},
                    {"id": "F2", "displayName": "Skräppost", "childFolderCount": 0}]
        self.children = [{"id": "F3", "displayName": "IT", "childFolderCount": 0}]
        self.f1_pages = (["A"], ["B"])  # the first pass over F1: two pages
        self.value_status = {}  # message id -> statuses its $value answers with before a 200
        self.expired = False  # the stored F1 delta link is no longer known to Graph

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "Talos must only ever GET from Graph"
        assert 'IdType="ImmutableId"' in request.headers["Prefer"]
        self.requests.append(request.url)
        path, q = request.url.path, str(request.url)
        if path.endswith("/me/mailFolders"):
            return httpx.Response(200, json={"value": self.top})
        if path.endswith("/mailFolders/F1/childFolders"):
            return httpx.Response(200, json={"value": self.children})
        if path.endswith("/messages/delta") and "F1" in path:
            if "page=2" in q:
                return httpx.Response(200, json={"value": [self.item(i) for i in self.f1_pages[1]],
                                                 "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta/F1?t=1"})
            if self.throttle_once:
                self.throttle_once = False
                return httpx.Response(429, headers={"Retry-After": "1"})
            return httpx.Response(200, json={"value": [self.item(i) for i in self.f1_pages[0]],
                                             "@odata.nextLink": str(request.url) + "&page=2"})
        if path.endswith("/delta/F1") and self.expired:
            return httpx.Response(410, json={"error": {"code": "SyncStateNotFound",
                                                       "message": "The sync state is not found."}})
        if path.endswith("/messages/delta") and "F3" in path:
            return httpx.Response(200, json={"value": [self.item("C")],
                                             "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta/F3?t=1"})
        if path.endswith("/delta/F1"):
            return httpx.Response(200, json={"value": [self.item("A", isRead=True, categories=["Talos/Kund"]),
                                                       {"id": "B", "@removed": {"reason": "deleted"}}],
                                             "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta/F1?t=2"})
        if path.endswith("/delta/F3"):
            return httpx.Response(200, json={"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta/F3?t=2"})
        if path.endswith("/$value"):
            mid = path.split("/")[-2]
            if self.value_status.get(mid):
                return httpx.Response(self.value_status[mid].pop(0), json={"error": {"code": "x"}})
            return httpx.Response(200, content=self.raw[mid])
        return httpx.Response(404, text=f"unexpected {request.url}")

    @staticmethod
    def item(i, isRead=False, categories=()):
        return {"id": i, "conversationId": "conv-" + i, "isRead": isRead, "isDraft": False,
                "flag": {"flagStatus": "notFlagged"}, "categories": list(categories),
                "receivedDateTime": "2026-09-22T08:14:00Z"}


def test_graph_backfill_then_delta_mirrors_changes_and_removals(conn, ingestor):
    fake = FakeGraph()
    slept = []
    client = GraphClient(lambda: "token", httpx.Client(transport=httpx.MockTransport(fake)), sleep=slept.append)
    source = GraphMailSource(client, skip_folders={"Skräppost"})
    s1 = base.run(conn, ingestor, "work", source)
    assert s1.added == 3 and slept == [1.0]
    assert {r["scope"] for r in conn.execute("select scope from sync_cursor")} == {"folder:F1", "folder:F3"}

    s2 = base.run(conn, ingestor, "work", source)
    assert s2.added == 0 and s2.updated == 1 and s2.gone == 1
    state = {r["subject"]: r for r in conn.execute(
        "select m.subject, l.flags, l.labels, l.present from message m join message_location l on l.message_id = m.id")}
    assert state["Hej från Graph"]["flags"] == ["seen"] and state["Hej från Graph"]["labels"] == ["Talos/Kund"]
    assert state["Andra"]["present"] is False
    assert not any("F2" in str(u) for u in fake.requests)  # skipped folder never read
    folders = {(r["folder"], r["folder_path"]) for r in conn.execute("select folder, folder_path from message_location")}
    assert folders == {("F1", "Inkorgen"), ("F3", "Inkorgen/IT")}  # keyed by id; the readable path for rules


def test_local_import_reads_eml_and_apple_mail_emlx(conn, ingestor, tmp_path):
    raw = mf.make(subject="Från Apple Mail")
    (tmp_path / "a.eml").write_bytes(mf.make(subject="Vanlig fil"))
    (tmp_path / "1234.emlx").write_bytes(f"{len(raw)}\n".encode() + raw + b"<?xml version='1.0'?><plist/>")
    s = base.run(conn, ingestor, "local", LocalSource([tmp_path]))
    assert s.added == 2
    assert {r["subject"] for r in conn.execute("select subject from message")} == {"Vanlig fil", "Från Apple Mail"}
    assert base.run(conn, ingestor, "local", LocalSource([tmp_path])).added == 0


def test_each_batch_is_committed_on_its_own_so_a_crash_keeps_earlier_batches(database, vault):
    """With an autocommit connection (as the CLI uses), a failure in batch 3 keeps batches 1–2."""
    from talos import db
    from talos.ingest import Ingestor
    from talos.sources import gmail as gmail_mod

    with db.connect(database, autocommit=True) as c:
        c.execute("truncate message, message_location, message_text, participant, thread, entity, blob,"
                  " sync_cursor, sync_run, fetch_failure, address, person, org restart identity cascade")
        fake = FakeGmail()
        for uid in range(1, 8):
            fake.add(uid, mf.make(subject=f"m{uid}"), msgid=5000 + uid, thrid=uid)
        fake.fail_on = 5  # the connection drops while batch 3 (UIDs 5–6) is fetched
        old = gmail_mod.BATCH
        gmail_mod.BATCH = 2
        try:
            import pytest
            with pytest.raises(Exception):
                base.run(c, Ingestor(c, vault), "gmail", GmailSource("x", lambda: "pw", client_factory=lambda: fake))
        finally:
            gmail_mod.BATCH = old
        with db.connect(database) as other:  # a separate session sees only committed work
            n = other.execute("select count(*) n from message where account_id = 'gmail'").fetchone()["n"]
            cur = other.execute("select state from sync_cursor where account_id = 'gmail'").fetchone()["state"]
        assert n == 4 and cur["last_uid"] == 4
        assert c.execute("select status from sync_run order by id desc limit 1").fetchone()["status"] == "failed"


# ---------------------------------------------------------------- IMAP robustness

def body_fetches(fake):
    return [list(uids) for uids, items, mods in fake.calls if "BODY.PEEK[]" in items and not mods]


def test_imaplib_can_read_a_search_answer_listing_200k_uids():
    import imaplib

    import talos.sources.gmail  # noqa: F401  (loading a source raises the line limit)

    line = b"* SEARCH " + b" ".join(str(u).encode() for u in range(1, 200_001)) + b"\r\n"
    assert len(line) > 1_000_000  # over imaplib's own limit

    class Sock:
        def recv(self, n):
            return b""

    conn = imaplib.IMAP4.__new__(imaplib.IMAP4)  # no network: only the line reader is exercised
    conn.sock, conn._readbuf = Sock(), [line]
    assert conn.readline() == line


def test_gmail_new_mail_is_searched_above_the_cursor_and_the_highest_uid_is_not_new(conn, ingestor):
    fake = FakeGmail()
    for uid in (1, 2, 5):
        fake.add(uid, mf.make(subject=f"m{uid}"), msgid=100 + uid, thrid=uid)
    gmail_sync(conn, ingestor, fake)
    fake.calls.clear()
    fake.searches.clear()
    assert gmail_sync(conn, ingestor, fake).added == 0
    assert ["UID", "6:*"] in fake.searches  # the server answers with UID 5, which is not new
    assert body_fetches(fake) == []


def test_gmail_uids_left_out_of_a_batch_fetch_are_fetched_again_one_by_one(conn, ingestor):
    fake = FakeGmail()
    for uid in (1, 2, 3):
        fake.add(uid, mf.make(subject=f"m{uid}"), msgid=100 + uid, thrid=uid)
    fake.left_out_of_batches = {2}
    assert gmail_sync(conn, ingestor, fake).added == 3
    assert [2] in body_fetches(fake)
    assert conn.execute("select count(*) n from fetch_failure").fetchone()["n"] == 0


def test_a_gmail_uid_the_server_never_returns_is_listed_and_fetched_on_a_later_run(conn, ingestor):
    fake = FakeGmail()
    for uid in (1, 2, 3):
        fake.add(uid, mf.make(subject=f"m{uid}"), msgid=100 + uid, thrid=uid)
    fake.never_returned = {2}
    s1 = gmail_sync(conn, ingestor, fake)
    assert (s1.added, s1.failed) == (2, 1)
    assert conn.execute("select ref from fetch_failure").fetchone()["ref"] == "[all]:7:2"
    assert conn.execute("select state from sync_cursor").fetchone()["state"]["last_uid"] == 3
    assert conn.execute("select status from sync_run order by id desc limit 1").fetchone()["status"] == "partial"

    fake.never_returned = set()
    assert gmail_sync(conn, ingestor, fake).added == 1
    assert conn.execute("select count(*) n from message").fetchone()["n"] == 3
    assert conn.execute("select count(*) n from fetch_failure").fetchone()["n"] == 0


def test_a_gmail_fetch_entry_without_a_body_or_message_id_is_listed_not_fatal_and_not_ingested(conn, ingestor):
    fake = FakeGmail()
    for uid in (1, 2, 3):
        fake.add(uid, mf.make(subject=f"m{uid}"), msgid=100 + uid, thrid=uid)
    fake.messages[2][b"BODY[]"] = None
    del fake.messages[3][b"X-GM-MSGID"]  # e.g. an unsolicited FETCH merged in under this number
    stats = gmail_sync(conn, ingestor, fake)
    assert (stats.added, stats.failed) == (1, 2)
    assert {r["provider_key"] for r in conn.execute("select provider_key from message")} == {"101"}
    assert {r["ref"] for r in conn.execute("select ref from fetch_failure")} == {"[all]:7:2", "[all]:7:3"}
    assert conn.execute("select state from sync_cursor").fetchone()["state"]["last_uid"] == 3


def test_gmail_catches_up_on_uids_an_earlier_run_skipped_below_the_cursor(conn, ingestor):
    fake = FakeGmail()
    for uid in (1, 2, 4, 5):
        fake.add(uid, mf.make(subject=f"m{uid}"), msgid=100 + uid, thrid=uid)
    gmail_sync(conn, ingestor, fake)
    fake.add(3, mf.make(subject="m3"), msgid=103, thrid=3)  # on the server all along; an old run skipped it
    stats = gmail_sync(conn, ingestor, fake)
    assert stats.added == 1
    assert conn.execute("select uid from message_location l join message m on m.id = l.message_id"
                        " where m.provider_key = '103'").fetchone()["uid"] == 3
    fake.calls.clear()
    assert gmail_sync(conn, ingestor, fake).added == 0 and body_fetches(fake) == []


def test_gmail_catch_up_leaves_a_message_that_failed_to_ingest_to_talos_retry(conn, ingestor, monkeypatch):
    from talos import ingest as ingest_mod

    real = ingest_mod.mime.parse
    monkeypatch.setattr(ingest_mod.mime, "parse", lambda raw: (_ for _ in ()).throw(ValueError("x"))
                        if b"BOOM" in raw else real(raw))
    fake = FakeGmail()
    fake.add(1, mf.make(subject="BOOM"), msgid=101, thrid=1)
    fake.add(2, mf.make(subject="fine"), msgid=102, thrid=2)
    assert gmail_sync(conn, ingestor, fake).failed == 1
    fake.calls.clear()
    gmail_sync(conn, ingestor, fake)
    assert body_fetches(fake) == []  # its original is in the vault already


def test_gmail_bodies_are_fetched_in_batches_bounded_by_size(conn, ingestor, monkeypatch):
    from talos.sources import gmail as gmail_mod

    fake = FakeGmail()
    for uid in range(1, 7):
        fake.add(uid, mf.make(subject=f"m{uid}", body="x" * (30_000 if uid == 3 else 10)), msgid=100 + uid, thrid=uid)
    small = fake.messages[1][b"RFC822.SIZE"]
    monkeypatch.setattr(gmail_mod, "MAX_BATCH_BYTES", 2 * small + 10)
    assert gmail_sync(conn, ingestor, fake).added == 6
    assert body_fetches(fake) == [[1, 2], [3], [4, 5], [6]]  # the large one alone
    assert any(items == ("RFC822.SIZE",) for _, items, _ in fake.calls)


class FakeImap:
    """Plain IMAP (iCloud, Loopia) as IMAPClient answers it. Fails on anything that writes."""

    def __init__(self):
        self.uidvalidity = 3
        self.folders = {"INBOX": {}}
        self.selected = None
        self.calls = []
        self.unsolicited = {}  # extra entries in a FLAGS answer, keyed by sequence number

    def add(self, folder, uid, raw, flags=()):
        self.folders.setdefault(folder, {})[uid] = {
            b"FLAGS": flags, b"INTERNALDATE": T0, b"BODY[]": raw,
            b"RFC822.SIZE": len(raw) if isinstance(raw, bytes) else 0}

    def list_folders(self):
        return [((b"\\HasNoChildren",), b"/", name) for name in self.folders]

    def select_folder(self, name, readonly=False):
        assert readonly, "Talos must select folders read-only"
        self.selected = name
        return {b"UIDVALIDITY": self.uidvalidity}

    def search(self, criteria):
        uids = sorted(self.folders[self.selected])
        if criteria == ["ALL"]:
            return uids
        low = int(criteria[1].split(":")[0])
        return sorted({u for u in uids if u >= low} | ({uids[-1]} if uids else set()))

    def fetch(self, uids, items, modifiers=None):
        assert "BODY[]" not in items, "bodies must be fetched with BODY.PEEK[]"
        self.calls.append((self.selected, list(uids), tuple(items)))
        box = self.folders[self.selected]
        out = {u: dict(box[u]) for u in uids if u in box}
        if list(items) == ["FLAGS"]:
            out.update(self.unsolicited)
        return out

    def logout(self):
        pass

    def __getattr__(self, name):
        raise AssertionError(f"Talos called IMAP {name}(); sync must never write")


def imap_sync(conn, ingestor, fake):
    from talos.sources.imap import ImapSource
    conn.execute("insert into account (id, provider, address) values ('icloud', 'imap', 'me@icloud.invalid')"
                 " on conflict do nothing")
    conn.commit()
    return base.run(conn, ingestor, "icloud", ImapSource("x", lambda: "pw", "imap.invalid",
                                                         client_factory=lambda: fake))


def test_plain_imap_does_not_fold_fetch_entries_without_a_body_into_one_empty_message(conn, ingestor):
    fake = FakeImap()
    fake.add("INBOX", 1, None)
    fake.add("INBOX", 2, None)
    fake.add("INBOX", 3, mf.make(subject="riktig"))
    stats = imap_sync(conn, ingestor, fake)
    assert (stats.added, stats.failed) == (1, 2)
    assert [r["subject"] for r in conn.execute("select subject from message")] == ["riktig"]
    assert {r["ref"] for r in conn.execute("select ref from fetch_failure")} == {"INBOX:3:1", "INBOX:3:2"}


def test_plain_imap_flag_refresh_ignores_answers_it_did_not_ask_for(conn, ingestor):
    fake = FakeImap()
    fake.add("INBOX", 10, mf.make(subject="a"))
    imap_sync(conn, ingestor, fake)
    fake.unsolicited = {1: {b"FLAGS": (b"\\Flagged",), b"SEQ": 1}}  # keyed by sequence number, no UID
    fake.folders["INBOX"][10][b"FLAGS"] = (b"\\Seen",)
    imap_sync(conn, ingestor, fake)
    assert conn.execute("select flags, folder_path from message_location").fetchone() == {
        "flags": ["seen"], "folder_path": "INBOX"}


def test_plain_imap_catches_up_on_skipped_uids_but_not_on_duplicates_every_run(conn, ingestor):
    fake = FakeImap()
    same = mf.make(subject="dubblett")
    fake.add("INBOX", 1, same)
    fake.add("INBOX", 2, same)  # the same bytes filed twice in one folder
    fake.add("INBOX", 4, mf.make(subject="fyra"))
    imap_sync(conn, ingestor, fake)
    fake.add("INBOX", 3, mf.make(subject="tre"))  # skipped by an earlier run
    assert imap_sync(conn, ingestor, fake).added == 1
    fake.calls.clear()
    imap_sync(conn, ingestor, fake)
    assert [c for c in fake.calls if "BODY.PEEK[]" in c[2]] == []
    assert conn.execute("select count(*) n from message").fetchone()["n"] == 3


# ---------------------------------------------------------------- Graph robustness

def graph_source(fake, **kw):
    client = GraphClient(lambda: "token", httpx.Client(transport=httpx.MockTransport(fake)), sleep=lambda s: None)
    return GraphMailSource(client, skip_folders=kw.pop("skip_folders", {"Skräppost"}), **kw)


def test_a_graph_message_deleted_before_its_download_does_not_block_the_folder(conn, ingestor):
    fake = FakeGraph()
    fake.value_status = {"B": [404]}
    stats = base.run(conn, ingestor, "work", graph_source(fake))
    assert stats.added == 2
    assert conn.execute("select state from sync_cursor where scope = 'folder:F1'").fetchone()["state"][
        "delta_link"].endswith("t=1")
    assert conn.execute("select count(*) n from fetch_failure").fetchone()["n"] == 0


def test_a_graph_message_that_cannot_be_downloaded_is_listed_and_fetched_next_run(conn, ingestor):
    fake = FakeGraph()
    fake.value_status = {"B": [500] * 6}  # still failing after every retry
    s1 = base.run(conn, ingestor, "work", graph_source(fake))
    assert (s1.added, s1.failed) == (2, 1)
    failure = conn.execute("select ref, location from fetch_failure").fetchone()
    assert failure["ref"] == "B" and failure["location"]["folder"] == "F1"
    assert "delta_link" in conn.execute("select state from sync_cursor where scope = 'folder:F1'").fetchone()["state"]

    s2 = base.run(conn, ingestor, "work", graph_source(fake))
    assert s2.added == 1
    assert conn.execute("select count(*) n from message where provider_key = 'B'").fetchone()["n"] == 1
    assert conn.execute("select count(*) n from fetch_failure").fetchone()["n"] == 0


def test_an_expired_graph_delta_token_restarts_the_folder_and_marks_what_it_no_longer_has_gone(conn, ingestor):
    fake = FakeGraph()
    base.run(conn, ingestor, "work", graph_source(fake))
    fake.expired = True
    fake.f1_pages = (["A"], [])  # B was deleted while the token was lost; no @removed will ever say so
    stats = base.run(conn, ingestor, "work", graph_source(fake))
    assert any("resync" in n for n in stats.notes)
    present = {r["provider_key"]: r["present"] for r in conn.execute(
        "select m.provider_key, l.present from message m join message_location l on l.message_id = m.id")}
    assert present == {"A": True, "B": False, "C": True}
    state = conn.execute("select state from sync_cursor where scope = 'folder:F1'").fetchone()["state"]
    assert state["delta_link"].endswith("t=1") and "resync_started" not in state


def test_graph_locations_keep_their_folder_id_through_a_rename_and_rules_see_the_new_path(conn, ingestor):
    from talos import rules

    fake = FakeGraph()
    base.run(conn, ingestor, "work", graph_source(fake))
    fake.children = [{"id": "F3", "displayName": "Drift", "childFolderCount": 0}]
    base.run(conn, ingestor, "work", graph_source(fake))
    loc = conn.execute("select l.folder, l.folder_path, l.present from message_location l"
                       " join message m on m.id = l.message_id where m.provider_key = 'C'").fetchone()
    assert loc == {"folder": "F3", "folder_path": "Inkorgen/Drift", "present": True}
    assert rules.preview(conn, [{"field": "folder", "op": "is", "value": "Inkorgen/Drift"}])["count"] == 1
    assert rules.preview(conn, [{"field": "folder", "op": "matches", "value": "(^|/)drift$"}])["count"] == 1


def test_a_graph_folder_that_disappears_has_its_mail_marked_gone(conn, ingestor):
    fake = FakeGraph()
    base.run(conn, ingestor, "work", graph_source(fake))
    fake.children = []
    fake.top[0]["childFolderCount"] = 0
    stats = base.run(conn, ingestor, "work", graph_source(fake))
    assert stats.gone >= 1
    assert conn.execute("select l.present from message_location l join message m on m.id = l.message_id"
                        " where m.provider_key = 'C'").fetchone()["present"] is False
    assert conn.execute("select count(*) n from sync_cursor where scope = 'folder:F3'").fetchone()["n"] == 0


def test_graph_skips_hidden_folders_and_everything_under_a_skipped_folder(conn, ingestor):
    fake = FakeGraph()
    fake.top[1]["isHidden"] = True  # F2, a hidden system folder
    source = graph_source(fake, skip_folders=set())
    assert [f["id"] for f in source.folders()] == ["F1", "F3"]
    assert [f["id"] for f in graph_source(fake, skip_folders=set(), include_hidden=True).folders()] == ["F1", "F3", "F2"]
    assert [f["id"] for f in graph_source(fake, skip_folders={"inkorgen"}).folders()] == []


def test_graph_honours_retry_after_as_a_date_retries_network_errors_and_refreshes_a_rejected_token():
    from email.utils import format_datetime
    from datetime import timedelta

    answers = ["network", "503-date", "401", "ok"]
    seen_tokens = []

    def handler(request):
        seen_tokens.append(request.headers["Authorization"])
        what = answers.pop(0)
        if what == "network":
            raise httpx.ConnectError("reset", request=request)
        if what == "503-date":
            when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
            return httpx.Response(503, headers={"Retry-After": when})
        if what == "401":
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})
        return httpx.Response(200, json={"value": []})

    slept = []
    client = GraphClient(lambda: "old", httpx.Client(transport=httpx.MockTransport(handler)), sleep=slept.append,
                         refresh=lambda: "new")
    assert client.get("/me/mailFolders") == {"value": []}
    assert len(slept) == 2 and 25 <= slept[1] <= 31
    assert seen_tokens[-1] == "Bearer new" and seen_tokens[-2] == "Bearer old"


def test_an_empty_graph_folder_list_is_not_taken_to_mean_every_folder_is_gone(conn, ingestor):
    fake = FakeGraph()
    base.run(conn, ingestor, "work", graph_source(fake))
    fake.top = []
    base.run(conn, ingestor, "work", graph_source(fake))
    assert {r["present"] for r in conn.execute("select present from message_location")} == {True}
    assert conn.execute("select count(*) n from sync_cursor where account_id = 'work'").fetchone()["n"] == 2


def test_graph_downloads_a_pages_new_messages_in_parallel_but_at_most_four_at_a_time(conn, ingestor):
    import threading
    import time as _time

    raws = {f"M{i}": mf.make(subject=f"parallel {i}", msgid=f"<p{i}@x>") for i in range(10)}
    state = {"now": 0, "max": 0}
    lock = threading.Lock()

    def handler(request):
        assert request.method == "GET"
        path = request.url.path
        if path.endswith("/me/mailFolders"):
            return httpx.Response(200, json={"value": [{"id": "F1", "displayName": "Inkorgen", "childFolderCount": 0}]})
        if path.endswith("/messages/delta"):
            return httpx.Response(200, json={"value": [FakeGraph.item(k) for k in raws],
                                             "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta/F1?t=1"})
        if path.endswith("/$value"):
            with lock:
                state["now"] += 1
                state["max"] = max(state["max"], state["now"])
            _time.sleep(0.05)
            with lock:
                state["now"] -= 1
            return httpx.Response(200, content=raws[path.split("/")[-2]])
        return httpx.Response(404)

    client = GraphClient(lambda: "token", httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    stats = base.run(conn, ingestor, "work", GraphMailSource(client))
    assert stats.added == 10
    assert 1 < state["max"] <= 4


def test_gmail_many_changes_are_found_window_by_window_as_gmail_fails_one_big_search(conn, ingestor):
    fake = FakeGmail()
    for uid in range(1, 7):
        fake.add(uid * 1000, mf.make(), msgid=uid, thrid=uid)
    gmail_sync(conn, ingestor, fake)
    gmail_sync(conn, ingestor, fake)  # records highestmodseq (100)
    fake.modseq = 100 + 60000       # tens of thousands of changes since, e.g. labels Talos wrote
    fake.max_modseq_answer = 2      # one big SEARCH MODSEQ fails, as Gmail's does
    fake.changed = {u * 1000: {b"X-GM-MSGID": u, b"X-GM-LABELS": (b"Talos/x",), b"FLAGS": ()} for u in range(1, 7)}
    gmail_sync(conn, ingestor, fake)
    labels = [r["labels"] for r in conn.execute("select labels from message_location order by uid")]
    assert labels == [["Talos/x"]] * 6
    assert ["MODSEQ", "100"] not in fake.searches[-5:]
    assert any(q[:1] == ["UID"] and "MODSEQ" in q for q in fake.searches)
