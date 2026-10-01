import json

import duckdb
import mailfactory as mf
import pytest

from talos import cases, events, export
from talos.ingest import Location


def ingest(ingestor, key, raw, **kw):
    return ingestor.ingest("gmail", raw, Location("[all]", key, **kw)).message_id


def test_machine_mail_becomes_events_with_a_status_read_from_the_text(conn, ingestor):
    auto = {"Auto-Submitted": "auto-generated"}
    ingest(ingestor, "b1", mf.make(frm="Veeam <noreply@backup.example>", subject="[Failed] Backup job NAS",
                                   body="Job finished with error.", headers=auto))
    ingest(ingestor, "b2", mf.make(frm="Veeam <noreply@backup.example>", subject="[Success] Backup job NAS",
                                   body="All good.", headers=auto))
    ingest(ingestor, "a1", mf.make(frm="Larmbolaget <larm@larmbolaget.example>", subject="Larm: inbrottslarm kontoret"))
    ingest(ingestor, "p1", mf.make(subject="Backup av mina foton?"))  # a person, not a machine
    conn.commit()
    assert events.run(conn) == {"backup": 2, "alarm": 1, "m365-alert": 0}
    assert events.run(conn) == {"backup": 0, "alarm": 0, "m365-alert": 0}  # idempotent
    rows = {r["fields"]["subject"]: r for r in conn.execute("select * from event")}
    assert rows["[Failed] Backup job NAS"]["status"] == "failed"
    assert rows["[Success] Backup job NAS"]["status"] == "ok"
    assert rows["[Failed] Backup job NAS"]["system"] == "Veeam"


def test_a_new_extractor_version_re_reads_history(conn, ingestor):
    ingest(ingestor, "b1", mf.make(frm="Veeam <noreply@backup.example>", subject="Backup job NAS",
                                   headers={"Auto-Submitted": "auto-generated"}))
    conn.commit()
    ex = events.Extractor("backup", "backup", events.DEFAULTS[0].conditions, version=1)
    events.run(conn, [ex])
    ex2 = events.Extractor("backup", "backup", events.DEFAULTS[0].conditions, version=2, default_status="unknown")
    assert events.run(conn, [ex2]) == {"backup": 1}
    assert conn.execute("select extractor_version, status from event").fetchone() == \
        {"extractor_version": 2, "status": "unknown"}


def test_case_records_go_out_and_only_valid_decisions_come_back_as_proposals(conn, ingestor, tmp_path):
    a = ingest(ingestor, "a", mf.make(subject="Kvitto Hemköp", body="Tack för ditt köp " * 200),
               provider_thread_id="T1")
    ingest(ingestor, "b", mf.make(subject="Vinn en iPhone!!!"), provider_thread_id="T2")
    conn.commit()
    run = cases.export(conn, tmp_path / "cases.jsonl", model="jev", purpose="topic", dimension="topic")
    records = [json.loads(l) for l in (tmp_path / "cases.jsonl").read_text().splitlines()]
    assert len(records) == 2 and len(records[0]["body_excerpt"]) <= cases.BODY_CHARS
    t1 = conn.execute("select thread_id from message where id = %s", (a,)).fetchone()["thread_id"]
    t2 = [r["case_id"] for r in records if r["case_id"] != t1][0]

    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"case_id": 999999, "value": "Orders"}) + "\n")
    with pytest.raises(cases.CaseError):
        cases.import_decisions(conn, run, bad)
    assert conn.execute("select count(*) n from assignment").fetchone()["n"] == 0  # nothing half-landed

    good = tmp_path / "good.jsonl"
    good.write_text(json.dumps({"case_id": t1, "value": "Family/Groceries", "confidence": 0.9,
                                "evidence": "Hemköp receipt"}) + "\n" + json.dumps({"case_id": t2, "hold": "spam"}) + "\n")
    assert cases.import_decisions(conn, run, good) == {"proposed": 1, "held": 1, "unanswered": 0}
    assert conn.execute("select count(*) n from effective_assignment").fetchone()["n"] == 0  # proposals are not active
    assert cases.accept(conn, run, min_confidence=0.8) == 1
    eff = conn.execute("select * from effective_assignment").fetchone()
    assert eff["entity_id"] == t1 and eff["value"] == "Family/Groceries" and eff["source_kind"] == "model"
    held = conn.execute("select status from assignment where value = 'hold:spam'").fetchone()
    assert held["status"] == "proposed"  # a spam hold is never auto-accepted


def test_snapshot_is_queryable_with_duckdb_and_ai_rows_are_one_line_per_thread(conn, ingestor, tmp_path):
    ingest(ingestor, "a", mf.make(subject="Ett"), labels=["Receipts"], provider_thread_id="T1")
    ingest(ingestor, "b", mf.make(subject="Två", frm="Other <x@example.org>"), provider_thread_id="T1")
    conn.commit()
    out = export.snapshot(conn, tmp_path / "exports")
    n = duckdb.sql(f"select count(*) from '{out}/messages.parquet' where list_contains(labels, 'Receipts')").fetchone()[0]
    assert n == 1
    assert export.ai_rows(conn, tmp_path / "rows.jsonl") == 1
    row = json.loads((tmp_path / "rows.jsonl").read_text())
    assert [m["from"] for m in row["messages"]] == ["oskar@nordvik.se", "x@example.org"]


def test_mail_filed_in_the_backup_folder_is_an_event_even_without_backup_in_the_subject(conn, ingestor):
    ingestor.ingest("work", mf.make(frm="Reports <reports@nas.example>", subject="[Success] NAS nightly"),
                    Location("Inkorgen/Backup", "x1"))
    ingestor.ingest("work", mf.make(frm="Reports <reports@nas.example>", subject="[Success] NAS nightly"),
                    Location("Inkorgen", "x2"))
    conn.commit()
    assert events.run(conn)["backup"] == 1
    assert conn.execute("select status from event").fetchone()["status"] == "ok"


@pytest.mark.parametrize("subject, body, expected", [
    ("[Success] Code Linux (12 machines)", "Processed: 12, Warnings: 0, Failed: 0", "ok"),
    ("[Warning] Manually Added (3 machines) 1 Warnings", "Failed: 0", "warning"),
    ("[Failed] Nightly job", "", "failed"),
    ("[nas.example] Active Backup - säkerhetskopieringen [Full 3] på [nas] slutfördes delvis", "", "warning"),
    ("[nas.example] Active Backup - säkerhetskopieringsuppgift Store på nas har slutförts", "", "ok"),
    ("[nas.example] Active Backup - säkerhetskopieringsuppgift All-New på nas misslyckades", "", "failed"),
    ("[Veeam ONE Monitor] Alarm - Heartbeat is missing for VM has been changed to Reset/resolved (previous state: Error)",
     "", "ok"),
    ("[Veeam ONE Monitor] Alarm - Heartbeat is missing for VM has been changed to Error (previous state: Reset/resolved)",
     "", "failed"),
    ("Network backup – Off site", "The job failed to reach the target.", "failed"),
    ("Network backup – Off site", "Summary: 0 errors, 0 warnings.", "info"),
])
def test_event_status_reads_the_subject_first_and_ignores_zero_counts(subject, body, expected):
    assert events.DEFAULTS[0].status(subject, body) == expected
