import mailfactory as mf

from talos import cli, mime, reparse
from talos.ingest import Location


def outdate(conn, message_id, **fields):
    """Make a row look as the previous parser left it."""
    conn.execute("update message set parser_version = 1, snippet = 'gammalt' where id = %s", (message_id,))
    conn.execute("update message_text set body_text = 'gammalt', quote_stripped = 'gammalt',"
                 " search = to_tsvector('simple', 'gammalt') where message_id = %s", (message_id,))
    for column, value in fields.items():
        conn.execute(f"update message set {column} = %s where id = %s", (value, message_id))


def test_reparse_refreshes_text_and_flags_and_adds_only_parts_it_did_not_have(conn, ingestor, vault):
    from talos import search
    mid = ingestor.ingest("gmail", mf.make(subject="Kvitton från resan", body="Här är kvittona från Oslo.", attachments=[
        ("kvitto.pdf", "application/pdf", mf.pdf("Kvitto 1")), ("IMG_0001.jpg", "image/jpeg", mf.jpeg())]),
        Location("[all]", "a", provider_thread_id="t1")).message_id
    kept = conn.execute("select id from attachment where filename = 'kvitto.pdf'").fetchone()["id"]
    conn.execute("delete from attachment where filename = 'IMG_0001.jpg'")  # a part the old parser missed
    outdate(conn, mid, has_attachments=False)
    before = {t: conn.execute(f"select count(*) n from {t}").fetchone()["n"] for t in ("person", "thread", "participant")}
    conn.commit()
    assert search.messages(conn, "oslo")["total"] == 0

    assert reparse.reparse(conn, vault) == {"reparsed": 1, "failed": 0, "remaining": 0}
    conn.commit()
    m = conn.execute("select m.*, t.body_text from message m join message_text t on t.message_id = m.id").fetchone()
    assert m["parser_version"] == mime.PARSER_VERSION and m["has_attachments"]
    assert m["body_text"] == "Här är kvittona från Oslo." and m["snippet"] == "Här är kvittona från Oslo."
    assert search.messages(conn, "oslo")["total"] == 1
    atts = {r["filename"]: r["id"] for r in conn.execute("select id, filename from attachment")}
    assert atts["kvitto.pdf"] == kept and "IMG_0001.jpg" in atts  # the old row untouched, the missing one added
    assert {t: conn.execute(f"select count(*) n from {t}").fetchone()["n"] for t in before} == before


def test_reparse_repairs_a_subject_stored_with_replacement_characters(conn, ingestor, vault):
    raw = ("From: Jörgen <jorgen@nordvik.se>\r\nTo: owner@gmail.com\r\nSubject: Räkning\r\n"
           "Content-Type: text/plain\r\n\r\nHär är den.\r\n").encode("latin-1")
    mid = ingestor.ingest("gmail", raw, Location("[all]", "a")).message_id
    outdate(conn, mid, subject="R�kning", from_name="J�rgen")
    conn.commit()
    reparse.reparse(conn, vault)
    conn.commit()
    m = conn.execute("select subject, from_name from message").fetchone()
    assert (m["subject"], m["from_name"]) == ("Räkning", "Jörgen")


def test_reparse_stops_at_its_limit_and_continues_later(conn, ingestor, vault):
    for key in ("a", "b", "c"):
        outdate(conn, ingestor.ingest("gmail", mf.make(subject=f"Brev {key}"), Location("[all]", key)).message_id)
    conn.commit()
    assert reparse.reparse(conn, vault, limit=2, batch=1) == {"reparsed": 2, "failed": 0, "remaining": 1}
    assert reparse.reparse(conn, vault) == {"reparsed": 1, "failed": 0, "remaining": 0}


def test_talos_reparse_runs_from_the_command_line(conn, ingestor, vault, database, monkeypatch, capsys):
    outdate(conn, ingestor.ingest("gmail", mf.make(), Location("[all]", "a")).message_id)
    conn.commit()
    monkeypatch.setenv("TALOS_DSN", database)
    monkeypatch.setenv("TALOS_HOME", str(vault.root.parent))
    monkeypatch.setattr(cli, "_logging", lambda settings, verbose: None)
    cli.main(["reparse", "--limit", "10"])
    assert f"reparsed 1 messages to parser version {mime.PARSER_VERSION}; failed 0; still older: 0" in capsys.readouterr().out
