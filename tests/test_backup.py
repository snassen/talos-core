"""The nightly backup of the owner's own work (talos.backup): what it holds, how it is keyed for a database
rebuilt from the vault, pruning, when it is due, and its Argus check-in. Invented mail only."""

import gzip
import json
import subprocess
from datetime import datetime, timedelta

import pytest
from test_enrich import mail

from talos import argus, backup, cli, config, objects

pytestmark = pytest.mark.usefixtures("taxonomy_loaded")


@pytest.fixture
def home(tmp_path, database):
    return config.Settings(home=tmp_path, dsn=database)


def _lines(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(x) for x in fh]


def _own_work(conn, ingestor):
    mid = mail(ingestor, frm="Pia Ek <pia@example.org>", subject="Offert", body="Hej", msgid="<offert@test.invalid>")
    other = mail(ingestor, frm="Bo <bo@example.org>", subject="Lunch", body="Ses")
    conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, confidence,"
                 " decided_by) values (%s, 'topic', 'Work', 'human', 'studio:1', 1.0, 'owner')", (mid,))
    conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, status, confidence)"
                 " values (%s, 'topic', 'Private', 'model', 'jev-test', 'proposed', 0.6)", (other,))
    binder = objects.create(conn, "project", "Offerten")
    objects.add(conn, binder, [mid])
    conn.commit()
    return mid, binder


def test_a_backup_holds_the_owners_work_keyed_to_survive_a_rebuild(conn, ingestor, home):
    mid, binder = _own_work(conn, ingestor)
    m = backup.run(home)
    day = home.home / "backups" / datetime.now().astimezone().strftime("%Y-%m-%d")
    assert m["path"] == str(day) and sorted(p.name for p in day.iterdir()) == [
        "manifest.json", "own-links.jsonl.gz", "own-values.jsonl.gz", "own.dump"]
    assert json.loads((day / "manifest.json").read_text())["values"] == 1 == m["values"]
    # only the owner's own value, with the message's stable keys beside its id
    [v] = _lines(day / "own-values.jsonl.gz")
    sha = conn.execute("select raw_sha256 from message where id = %s", (mid,)).fetchone()["raw_sha256"]
    assert (v["value"], v["source_kind"]) == ("Work", "human")
    assert v["target"] == {"entity": mid, "kind": "message", "raw_sha256": sha, "message_id": "offert@test.invalid"}
    # the binder membership, but none of the links ingest made
    links = _lines(day / "own-links.jsonl.gz")
    assert [(x["rel"], x["src"], x["dst"], x["src_message"]["raw_sha256"]) for x in links] == [
        ("member_of", mid, binder, sha)]
    assert m["tables"]["object"] == 1 and "message" not in m["tables"]
    # the dump is a real pg_dump archive of the own tables, and only those
    toc = subprocess.run([backup.pg_dump().replace("pg_dump", "pg_restore"), "-l", str(day / "own.dump")],
                         capture_output=True, text=True, check=True).stdout
    assert " TABLE public object " in toc and " TABLE public message " not in toc
    assert oct((day / "own.dump").stat().st_mode)[-3:] == "600"


def test_a_second_run_the_same_day_replaces_it_and_old_ones_are_pruned_but_never_the_last_three(home):
    root = home.home / "backups"
    for d in ("2026-08-01", "2026-08-02", "2026-09-20", "2026-09-29"):
        (root / d).mkdir(parents=True)
        (root / d / "manifest.json").write_text("{}")
    (root / ".2026-09-30.partial").mkdir()           # an interrupted run is not a backup
    now = datetime(2026, 10, 1, 3, 0).astimezone()
    assert backup.prune(home, now=now) == ["2026-08-01"]   # 08-02 is old too, but one of the newest three
    assert [p.name for p in backup.backups(home)] == ["2026-09-29", "2026-09-20", "2026-08-02"]
    assert backup.run(home, now=now)["pruned"] == ["2026-08-02"]   # a fourth one makes it one too many
    assert backup.run(home, now=now)["pruned"] == []
    assert [p.name for p in backup.backups(home)] == ["2026-10-01", "2026-09-29", "2026-09-20"]


def test_it_is_due_once_a_day_after_two(home):
    at = lambda h, d=1: datetime(2026, 10, d, h, 30).astimezone()  # noqa: E731
    assert not backup.due(home, at(1))
    assert backup.due(home, at(2))
    (home.home / "backups" / "2026-10-01").mkdir(parents=True)
    (home.home / "backups" / "2026-10-01" / "manifest.json").write_text("{}")
    assert not backup.due(home, at(23))
    assert backup.due(home, at(2, d=2))


def test_the_backup_checks_in_with_argus_and_a_failure_is_a_failing_check_in(conn, home, monkeypatch, capsys):
    argus.seed(conn)
    last = lambda: conn.execute("select ok, expected_next_at - at as within from argus_checkin"  # noqa: E731
                                " where slug = 'talos-backup' order by id desc limit 1").fetchone()
    cli._backup(home)
    assert "backup: " in capsys.readouterr().out
    assert last() == {"ok": True, "within": timedelta(hours=26)}
    monkeypatch.setattr(backup, "pg_dump", lambda: "/nonexistent/pg_dump")
    cli._backup(home)                                 # quiet: the sync that runs it never fails
    assert last()["ok"] is False
    assert not list((home.home / "backups").glob(".*.partial"))
