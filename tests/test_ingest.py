import hashlib

import mailfactory as mf
import pytest

from talos.ingest import Location, extract_pending, Ingestor


def loc(key, **kw):
    return Location(folder=kw.pop("folder", "[all]"), provider_key=key, **kw)


def test_the_vault_keeps_the_exact_original_and_refuses_a_tampered_one(vault):
    raw = mf.make()
    ref = vault.put(raw, "raw")
    assert ref.sha256 == hashlib.sha256(raw).hexdigest()
    assert ref.codec == "zstd" and ref.stored_size < ref.size
    assert vault.get(ref.sha256, "raw") == raw
    assert vault.put(raw, "raw") == ref  # writing it again is a no-op
    path = vault.root / ref.path
    path.write_bytes(path.read_bytes()[:-5] + b"xxxxx")
    with pytest.raises(Exception):
        vault.get(ref.sha256, "raw")


def test_ingest_creates_the_message_its_text_people_org_and_thread(conn, ingestor):
    res = ingestor.ingest("gmail", mf.make(subject="Brandväggsfönster fredag?"),
                          loc("m1", flags=["seen"], labels=["\\Inbox"], provider_thread_id="t1"))
    conn.commit()
    assert res.created
    m = conn.execute("select * from message where id = %s", (res.message_id,)).fetchone()
    assert m["direction"] == "in" and m["from_address"] == "oskar@nordvik.se"
    assert conn.execute("select count(*) n from message_text").fetchone()["n"] == 1
    person = conn.execute("select * from person where primary_address = 'oskar@nordvik.se'").fetchone()
    org = conn.execute("select * from org where id = %s", (person["org_id"],)).fetchone()
    assert org["domain"] == "nordvik.se"
    me = conn.execute("select is_me, org_id from person where primary_address = 'owner@gmail.com'").fetchone()
    assert me["is_me"] and me["org_id"] is None  # gmail.com is a mail provider, not an employer
    rels = {r["rel"] for r in conn.execute("select rel from edge where src = %s", (res.message_id,))}
    assert rels == {"in_thread", "from", "to"}
    location = conn.execute("select flags, labels from message_location").fetchone()
    assert location["flags"] == ["seen"] and location["labels"] == ["\\Inbox"]


def test_seeing_a_message_again_only_refreshes_its_server_state(conn, ingestor):
    first = ingestor.ingest("gmail", mf.make(), loc("m1", flags=[]))
    again = ingestor.ingest("gmail", mf.make(), loc("m1", flags=["seen", "flagged"], labels=["Receipts"]))
    conn.commit()
    assert not again.created and again.message_id == first.message_id
    assert conn.execute("select count(*) n from message").fetchone()["n"] == 1
    location = conn.execute("select flags, labels from message_location").fetchone()
    assert location["flags"] == ["flagged", "seen"] and location["labels"] == ["Receipts"]


def test_direction_is_decided_by_my_own_addresses(conn, ingestor):
    out = ingestor.ingest("gmail", mf.make(frm="Alex <owner@gmail.com>", to="oskar@nordvik.se"),
                          loc("out"))
    to_self = ingestor.ingest("gmail", mf.make(frm="o@company.example", to="owner@gmail.com",
                                               subject="note to self"), loc("self"))
    conn.commit()
    d = {r["id"]: r["direction"] for r in conn.execute("select id, direction from message")}
    assert d[out.message_id] == "out" and d[to_self.message_id] == "self"


def test_replies_join_the_thread_of_the_message_they_answer(conn, ingestor):
    a = ingestor.ingest("work", mf.make(msgid="<root@nordvik.se>", subject="Offert"), loc("a", folder="inbox"))
    b = ingestor.ingest("work", mf.make(frm="o@company.example", msgid="<r1@work>", subject="SV: Offert",
                                       in_reply_to="<root@nordvik.se>", references="<root@nordvik.se>"),
                        loc("b", folder="sent"))
    c = ingestor.ingest("work", mf.make(msgid="<other@nordvik.se>", subject="Annat"), loc("c", folder="inbox"))
    conn.commit()
    t = {r["id"]: r["thread_id"] for r in conn.execute("select id, thread_id from message")}
    assert t[a.message_id] == t[b.message_id] != t[c.message_id]
    thread = conn.execute("select message_count from thread where id = %s", (t[a.message_id],)).fetchone()
    assert thread["message_count"] == 2


def test_an_attachment_carried_by_two_messages_is_stored_once(conn, ingestor, vault):
    pdf = mf.pdf("Faktura 1234 Belopp 450 kr")
    for i in range(2):
        ingestor.ingest("gmail", mf.make(subject=f"Faktura {i}", attachments=[("faktura.pdf", "application/pdf", pdf)]),
                        loc(f"m{i}"))
    conn.commit()
    assert conn.execute("select count(*) n from attachment").fetchone()["n"] == 2
    assert conn.execute("select count(*) n from blob where kind = 'attachment'").fetchone()["n"] == 1
    att = conn.execute("select * from attachment limit 1").fetchone()
    assert att["extract_status"] == "ok" and "Faktura 1234" in att["extracted_text"]
    assert att["attrs"]["pages"] == 1
    assert vault.get(att["blob_sha256"], "attachment") == pdf


def test_spreadsheets_and_photos_are_read_for_their_contents(conn, ingestor):
    ingestor.ingest("gmail", mf.make(attachments=[
        ("kvitton.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         mf.xlsx([["Datum", "Belopp"], ["2026-09-01", 450]])),
        ("IMG_0001.jpg", "image/jpeg", mf.jpeg())]), loc("m1"))
    conn.commit()
    by_name = {r["filename"]: r for r in conn.execute("select * from attachment")}
    assert "Belopp" in by_name["kvitton.xlsx"]["extracted_text"]
    assert by_name["kvitton.xlsx"]["attrs"]["sheets"] == ["Kvitton"]
    assert by_name["IMG_0001.jpg"]["attrs"]["model"] == "iPhone 15"


def test_extraction_can_be_deferred_and_run_later(conn, vault):
    ing = Ingestor(conn, vault, extract_inline=False)
    ing.ingest("gmail", mf.make(attachments=[("a.pdf", "application/pdf", mf.pdf("Hello"))]), loc("m1"))
    conn.commit()
    assert conn.execute("select extract_status from attachment").fetchone()["extract_status"] == "pending"
    assert extract_pending(conn, vault) == 1
    conn.commit()
    assert conn.execute("select extract_status from attachment").fetchone()["extract_status"] == "ok"


def test_search_understands_swedish_and_english_word_forms(conn, ingestor):
    ingestor.ingest("gmail", mf.make(subject="Din faktura från Telia", body="Betala senast 30 september."), loc("a"))
    ingestor.ingest("gmail", mf.make(subject="Your invoice", body="Payment due."), loc("b"))
    ingestor.ingest("gmail", mf.make(subject="Middag på lördag", body="Kommer ni?"), loc("c"))
    conn.commit()

    def hits(q):
        return {r["subject"] for r in conn.execute(
            "select m.subject from message m join message_text t on t.message_id = m.id"
            " where t.search @@ (websearch_to_tsquery('swedish', %(q)s) || websearch_to_tsquery('english', %(q)s))",
            {"q": q})}

    assert hits("fakturor") == {"Din faktura från Telia"}
    assert hits("invoices") == {"Your invoice"}
    assert hits("oskar@nordvik.se") == {"Din faktura från Telia", "Your invoice", "Middag på lördag"}


def test_search_finds_swedish_word_forms_the_stemmer_keeps_apart(conn, ingestor):
    from talos import search
    ingestor.ingest("gmail", mf.make(subject="Ditt kvitto från Hemköp"), loc("a"))
    ingestor.ingest("gmail", mf.make(subject="Larmet har återställts"), loc("b"))
    ingestor.ingest("gmail", mf.make(subject="Middag på lördag"), loc("c"))
    conn.commit()
    subjects = lambda q: {r["subject"] for r in search.messages(conn, q)["rows"]}
    assert subjects("kvitton") == {"Ditt kvitto från Hemköp"}
    assert subjects("larm") == {"Larmet har återställts"}
    assert subjects('"kvitto från"') == {"Ditt kvitto från Hemköp"}
    assert search.prefix_query("kvitton hemköp") == "kvitt:* & hemk:*"


def test_nul_bytes_in_real_mail_do_not_stop_ingest(conn, ingestor, vault):
    raw = mf.make(subject="Hej\x00då", body="Text med\x00 NUL", attachments=[
        ("fil\x00.txt", "text/plain", b"inne\x00hall")])
    res = ingestor.ingest("gmail", raw, loc("nul"))
    conn.commit()
    m = conn.execute("select m.subject, t.body_text from message m join message_text t on t.message_id = m.id"
                     " where m.id = %s", (res.message_id,)).fetchone()
    assert m["subject"] == "Hejdå" and "\x00" not in m["body_text"]
    assert vault.get(conn.execute("select raw_sha256 from message").fetchone()["raw_sha256"], "raw") == raw
    assert conn.execute("select extracted_text from attachment").fetchone()["extracted_text"] == "innehall"


def test_one_message_that_cannot_be_ingested_is_kept_and_listed_not_fatal(conn, ingestor, vault, monkeypatch):
    from talos import ingest as ingest_mod
    from talos import mime

    real = mime.parse
    def fragile(raw):
        if b"BOOM" in raw:
            raise ValueError("parser tripped")
        return real(raw)
    monkeypatch.setattr(ingest_mod.mime, "parse", fragile)
    good = ingestor.ingest("gmail", mf.make(subject="fine"), loc("ok"))
    bad = ingestor.ingest("gmail", mf.make(subject="BOOM"), loc("bad", uid=9, labels=["X"]))
    conn.commit()
    assert good.created and bad.failed and bad.message_id is None
    failure = conn.execute("select * from ingest_failure").fetchone()
    assert failure["provider_key"] == "bad" and "parser tripped" in failure["error"]
    assert vault.exists(failure["raw_sha256"], "raw")  # the original is kept

    monkeypatch.setattr(ingest_mod.mime, "parse", real)  # the parser is fixed
    assert ingest_mod.retry_failures(conn, vault) == {"fixed": 1, "still_failing": 0}
    conn.commit()
    assert conn.execute("select count(*) n from ingest_failure").fetchone()["n"] == 0
    assert conn.execute("select labels from message_location l join message m on m.id = l.message_id"
                        " where m.provider_key = 'bad'").fetchone()["labels"] == ["X"]


def test_input_without_a_body_is_listed_as_a_fetch_failure_and_the_batch_carries_on(conn, ingestor):
    with conn.transaction():  # the adapter's batch
        bad = ingestor.ingest("gmail", None, loc("no-body", uid=6))
        good = ingestor.ingest("gmail", mf.make(subject="efter"), loc("after"))
    assert bad.failed and good.created
    failure = conn.execute("select ref, location, error from fetch_failure").fetchone()
    assert failure["ref"] == "no-body" and failure["location"]["uid"] == 6 and "bytes" in failure["error"]
    assert conn.execute("select count(*) n from ingest_failure").fetchone()["n"] == 0


def test_two_syncs_that_meet_the_same_new_sender_at_once_both_keep_their_message(conn, database, vault):
    """Two account syncs run side by side; both see a first mail from a new domain."""
    import threading
    import time

    from talos import db

    with db.connect(database) as a, db.connect(database) as b:
        first = Ingestor(a, vault).ingest("gmail", mf.make(frm="Ny <ny@nyfirma.se>", subject="A"), loc("a"))
        result = {}
        t = threading.Thread(target=lambda: result.update(r=Ingestor(b, vault).ingest(
            "work", mf.make(frm="Ny <ny@nyfirma.se>", subject="B"), loc("b"))))
        t.start()
        for _ in range(100):  # until b waits on a's uncommitted org row
            if conn.execute("select count(*) n from pg_stat_activity where datname = current_database()"
                            " and wait_event_type = 'Lock'").fetchone()["n"]:
                break
            time.sleep(0.05)
        a.commit()
        t.join(10)
        b.commit()
    assert first.created and result["r"].created and not result["r"].failed
    conn.commit()
    assert conn.execute("select count(*) n from org where domain = 'nyfirma.se'").fetchone()["n"] == 1
    assert conn.execute("select count(*) n from person where primary_address = 'ny@nyfirma.se'").fetchone()["n"] == 1
    assert conn.execute("select count(*) n from ingest_failure").fetchone()["n"] == 0
    assert conn.execute("select count(*) n from entity e where kind = 'org'"
                        " and not exists (select 1 from org o where o.id = e.id)").fetchone()["n"] == 0


def test_the_sync_migration_rekeys_graph_locations_on_a_database_that_already_holds_mail(database):
    """003 runs on the real database on top of 001 and 002, with mail already in it."""
    from psycopg import conninfo

    from talos import db

    dsn = conninfo.make_conninfo(database, dbname=conninfo.conninfo_to_dict(database)["dbname"] + "_mig")
    db.drop_database(dsn)
    db.ensure_database(dsn)
    try:
        with db.connect(dsn) as c:
            for name, sql in db.migrations():
                if name < "003":
                    c.execute(sql)
            c.execute("insert into account (id, provider, address) values ('work', 'graph', 'x@work.invalid'),"
                      " ('gmail', 'gmail', 'y@gmail.invalid')")
            c.execute("insert into sync_cursor (account_id, scope, state) values"
                      " ('work', 'folder:F1', '{\"path\": \"Inkorgen\", \"folder_id\": \"F1\", \"delta_link\": \"x\"}'),"
                      " ('work', 'folder:F3', '{\"path\": \"Inkorgen/Backup\", \"folder_id\": \"F3\"}')")
            for i, (acct, folder) in enumerate([("work", "Inkorgen"), ("work", "Inkorgen/Backup"),
                                                ("work", "Omdöpt sedan dess"), ("gmail", "[all]")], 1):
                c.execute("insert into entity (id, kind) values (%s, 'message')", (i,))
                c.execute("insert into message (id, account_id, provider_key, direction, parser_version)"
                          " values (%s, %s, %s, 'in', 1)", (i, acct, f"k{i}"))
                c.execute("insert into message_location (message_id, account_id, folder) values (%s, %s, %s)",
                          (i, acct, folder))
            c.commit()
            with c.transaction():
                c.execute(dict(db.migrations())["003_sync_robustness.sql"])
            rows = {r["message_id"]: (r["folder"], r["folder_path"]) for r in c.execute(
                "select message_id, folder, folder_path from message_location")}
        assert rows == {1: ("F1", "Inkorgen"), 2: ("F3", "Inkorgen/Backup"),
                        3: ("Omdöpt sedan dess", "Omdöpt sedan dess"), 4: ("[all]", "[all]")}
    finally:
        db.drop_database(dsn)


def test_has_attachments_counts_only_what_the_sender_attached(conn, ingestor):
    ids = {
        "invite": ingestor.ingest("gmail", mf.raw('multipart/alternative; boundary="A"', mf.INVITE, subject="Möte"),
                                  loc("invite")).message_id,
        "logo": ingestor.ingest("gmail", mf.raw('multipart/related; boundary="R"', mf.LOGO, subject="Nyhetsbrev"),
                                loc("logo")).message_id,
        "pdf": ingestor.ingest("gmail", mf.raw('multipart/mixed; boundary="B"', mf.INLINE_PDF, subject="Faktura"),
                               loc("pdf")).message_id,
    }
    conn.commit()
    flags = {k: conn.execute("select has_attachments from message where id = %s", (v,)).fetchone()["has_attachments"]
             for k, v in ids.items()}
    assert flags == {"invite": False, "logo": False, "pdf": True}


def test_a_utf_7_body_with_a_lone_surrogate_is_ingested(conn, ingestor):
    res = ingestor.ingest("gmail", mf.raw("text/plain; charset=utf-7", "Hej +2AA- d+AOU-"), loc("utf7"))
    conn.commit()
    assert res.created and not res.failed
    assert conn.execute("select body_text from message_text").fetchone()["body_text"] == "Hej � då"


def test_an_encrypted_pdf_attachment_is_stored_as_encrypted(conn, ingestor):
    ingestor.ingest("gmail", mf.make(attachments=[("lön.pdf", "application/pdf", mf.encrypted_pdf("Lön", "hemligt"))]),
                    loc("enc"))
    conn.commit()
    att = conn.execute("select extract_status, attrs from attachment").fetchone()
    assert att["extract_status"] == "encrypted" and att["attrs"]["encrypted"] is True


def test_a_deeply_nested_message_is_ingested_with_its_headers(conn, ingestor):
    res = ingestor.ingest("gmail", mf.nested(1000), loc("deep"))
    conn.commit()
    assert res.created
    m = conn.execute("select subject, headers from message").fetchone()
    assert m["subject"] == "Djupt" and "parse:RecursionError" in m["headers"]["defects"]


def test_a_forwarded_message_is_found_by_its_text_and_kept_byte_for_byte(conn, ingestor, vault):
    from talos import search
    ingestor.ingest("gmail", mf.forwarding(mf.INNER), loc("fw"))
    conn.commit()
    assert [r["subject"] for r in search.messages(conn, "offerten")["rows"]] == ["VB: Offert"]
    blobs = [r["blob_sha256"] for r in conn.execute("select blob_sha256 from attachment order by part_path")]
    assert vault.get(blobs[0], "attachment") == mf.INNER


def test_overlong_subjects_people_and_filenames_are_cut_for_the_search_vector(conn):
    from talos.ingest import SEARCH_SQL
    huge = " ".join(f"ord{i}" for i in range(150_000))  # > 1 MB of distinct words: too long for a tsvector
    for field in ("subject", "people", "filenames"):
        params = {"subject": "Hej", "people": "Oskar oskar@nordvik.se", "filenames": "a.pdf", "body": "text"}
        params[field] = huge
        vector = conn.execute(f"select {SEARCH_SQL} as v", params).fetchone()["v"]
        assert "ord0" in vector and "ord149999" not in vector


def test_a_vault_file_removed_by_security_software_is_recorded_not_fatal(conn, vault, database):
    """Defender deleted a phishing HTML attachment from the vault on the first night: extraction
    must mark it and carry on, and the web app must say what happened instead of failing."""
    from starlette.testclient import TestClient

    from talos.config import Settings
    from talos.web import app

    ing = Ingestor(conn, vault, extract_inline=False)
    ing.ingest("gmail", mf.make(attachments=[("Remittance Advice.HTML", "text/plain", b"<html>phish</html>"),
                                             ("ok.txt", "text/plain", b"fine")]), loc("phish"))
    conn.commit()
    bad = conn.execute("select id, blob_sha256 from attachment where filename like 'Remittance%%'").fetchone()
    (vault.root / vault._rel("attachment", bad["blob_sha256"])).unlink()  # what Defender did
    assert extract_pending(conn, vault) == 2
    conn.commit()
    rows = {r["filename"]: r for r in conn.execute("select filename, extract_status, attrs from attachment")}
    assert rows["ok.txt"]["extract_status"] == "ok"
    assert rows["Remittance Advice.HTML"]["extract_status"] == "error"
    assert rows["Remittance Advice.HTML"]["attrs"]["unavailable"] is True
    c = TestClient(app.create(Settings(home=vault.root.parent, dsn=database), allowed_hosts=["testserver"]))
    r = c.get(f"/api/attachments/{bad['id']}")
    assert r.status_code == 410 and "security software" in r.text
