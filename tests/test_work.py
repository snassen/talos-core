import mailfactory as mf
import pytest

from talos import objects, work
from talos.ingest import Location


def test_a_work_item_is_created_linked_to_its_message_and_every_change_is_recorded(conn, ingestor):
    mid = ingestor.ingest("gmail", mf.make(subject="Brandväggsfönster fredag?"), Location("[all]", "m1")).message_id
    project = objects.create(conn, "project", "FortiDLP Renewal")
    wid = work.create(conn, "Answer Oskar about the change window", home_id=project, message_ids=[mid])
    work.update(conn, wid, status="next", due="2026-09-30")
    work.update(conn, wid, status="done")
    item = work.get(conn, wid)
    assert item["status"] == "done" and item["done_at"] is not None and item["home_name"] == "FortiDLP Renewal"
    assert [m["id"] for m in item["messages"]] == [mid]
    assert [(e["field"], e["new_value"]) for e in item["history"]][1:] == [
        ("status", "next"), ("due", "2026-09-30"), ("status", "done")]
    assert [w["title"] for w in work.of_message(conn, mid)] == ["Answer Oskar about the change window"]


def test_an_unchanged_update_writes_no_event_and_bad_input_is_refused(conn):
    wid = work.create(conn, "Något att göra")
    work.update(conn, wid, status="inbox")
    assert len(work.get(conn, wid)["history"]) == 1
    with pytest.raises(work.WorkError):
        work.update(conn, wid, status="waiting")
    with pytest.raises(work.WorkError):
        work.update(conn, wid, due="nästa vecka")
    with pytest.raises(work.WorkError):
        work.create(conn, "  ")
    with pytest.raises(work.WorkError):
        work.update(conn, wid, home_id=999999999)


def test_the_obsidian_binder_kinds_are_object_kinds(conn):
    for kind in ("personal_project", "topic", "system"):
        oid = objects.create(conn, kind, f"A {kind}")
        assert conn.execute("select kind from object where id = %s", (oid,)).fetchone()["kind"] == kind
