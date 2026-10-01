"""Machine-mail clusters (talos.clusters, Operations › Clusters): the status of a cluster, the page's
numbers, and cleanup changesets that are only ever planned. Invented mail only."""

import time
from datetime import datetime, timedelta, timezone
from itertools import count

import bulkmail
import mailfactory as mf
import pytest
from test_gold import POST
from test_web import client

from talos import changesets, clusters
from talos.ingest import Location

AUTO = {"Auto-Submitted": "auto-generated"}
NOW = datetime.now(timezone.utc)
_n = count(1)


def _put(ingestor, account, frm, subject, days_ago, *, folder="[all]", labels=(), auto=True):
    n = next(_n)
    at = NOW - timedelta(days=days_ago, seconds=n)
    raw = mf.make(frm=frm, subject=subject, body=f"rad {n}", date=at, msgid=f"<c{n}@test.invalid>",
                  headers=AUTO if auto else {})
    return ingestor.ingest(account, raw, Location(folder, f"c{n}", uid=n, uidvalidity=1, provider_id=f"c{n}",
                                                  labels=list(labels), received_at=at)).message_id


def _mailbox(conn, ingestor) -> dict:
    """Gmail: a dead system (25 mails two years ago, ten still in the inbox), a quiet one (20 old and
    5 this year), a busy one, a shared sender split by system, a tiny one, and a friend (not machine
    mail). Work: a dead alarm system in the folder Alarm."""
    ids = {"dead": [_put(ingestor, "gmail", "Old <old@system.example>", f"Nightly report {i}", 800 + i,
                         labels=["\\Inbox"] if i < 10 else ["Reports"]) for i in range(25)],
           "quiet": [_put(ingestor, "gmail", "Quiet <q@quiet.example>", f"Offer {i}", 400 + 10 * i) for i in range(20)]
           + [_put(ingestor, "gmail", "Quiet <q@quiet.example>", f"Offer new {i}", 30 + 20 * i) for i in range(5)],
           "busy": [_put(ingestor, "gmail", "Busy <b@busy.example>", f"Alert {i}", i, labels=["\\Inbox"])
                    for i in range(30)],
           "ok": [_put(ingestor, "gmail", "Robot <robot@x.example>", f"[Success] Job {i}", 500 + i) for i in range(21)],
           "fail": [_put(ingestor, "gmail", "Robot <robot@x.example>", f"[Failed] Job {i}", 3 + i) for i in range(22)],
           "tiny": [_put(ingestor, "gmail", "Tiny <t@tiny.example>", "Hello", 900 + i) for i in range(3)],
           "friend": [_put(ingestor, "gmail", "Vän <van@example.org>", f"Hej {i}", 900 + i, auto=False)
                      for i in range(25)],
           "edge-fw": [_put(ingestor, "work", "Edge-fw <firewall@company.example>", f"Larm {i}", 3000 + i, folder="Alarm")
                       for i in range(22)]}
    conn.commit()
    return ids


def test_a_cluster_is_dead_after_twelve_silent_months_quiet_under_one_a_month_and_active_otherwise():
    now = NOW
    assert clusters.status_of(now - timedelta(days=366), 0, now) == "dead"
    assert clusters.status_of(None, 0, now) == "dead"
    assert clusters.status_of(now - timedelta(days=300), 11, now) == "quiet"
    assert clusters.status_of(now - timedelta(days=2), 12, now) == "active"


def test_the_page_lists_machine_clusters_dead_first_by_size_with_where_they_sit_and_what_cleaning_does(
        conn, ingestor, vault, database):
    _mailbox(conn, ingestor)
    p = clusters.compute(conn)
    got = [(c["account"], c["sender"], c["system"], c["count"], c["status"]) for c in p["clusters"]]
    assert got == [("gmail", "old@system.example", None, 25, "dead"),
                   ("work", "firewall@company.example", None, 22, "dead"),
                   ("gmail", "robot@x.example", "[Success]", 21, "dead"),
                   ("gmail", "q@quiet.example", None, 25, "quiet"),
                   ("gmail", "b@busy.example", None, 30, "active"),
                   ("gmail", "robot@x.example", "[Failed]", 22, "active")]   # the friend is not machine mail
    assert p["small"] == {"clusters": 1, "messages": 3}
    dead = p["clusters"][0]
    assert dead["in_inbox"] == 10 and dict(dead["places"]) == {"\\Inbox": 10, "Reports": 15}
    assert dead["recent"] == 0 and dead["last_year"] == 0 and len(dead["examples"]) == 2
    assert dict(p["clusters"][1]["places"]) == {"Alarm": 22}
    s = p["summary"]
    assert s["dead_clusters"] == 3 and s["dead_messages"] == 25 + 22 + 21 and s["dead_in_inbox"] == 10
    g = s["accounts"]["gmail"]
    assert (g["inbox_now"], g["inbox_after"]) == (40, 30)
    assert {x["place"]: (x["now"], x["dead"]) for x in g["places"]} == {"\\Inbox": (40, 10), "Reports": (15, 15)}
    st = client(database, vault).get("/api/clusters").json()
    assert [c["key"] for c in st["clusters"]][:2] == ["gmail|old@system.example|", "work|firewall@company.example|"]
    assert st["providers"]["work"] == "graph" and "planned only" in st["consent"] and "30 days" in st["trash_note"]
    ex = client(database, vault).get("/api/clusters/examples", params={"key": "gmail|robot@x.example|[Success]"}).json()
    assert ex["total"] == 21 and ex["rows"][0]["subject"] == "[Success] Job 0"      # newest first


def test_cleanup_is_planned_only_archive_labels_then_archives_the_inbox_trash_is_one_changeset_and_twice_adds_nothing(
        conn, ingestor):
    ids = _mailbox(conn, ingestor)
    res = clusters.prepare_cleanup(conn, "gmail|old@system.example|", "archive")
    conn.commit()
    made = res["changesets"]
    assert [(c["op"], c["args"], c["will_change"]) for c in made] == [
        ("add_label", {"label": clusters.CLEANUP_LABEL}, 25), ("archive", {}, 10)]
    rows = conn.execute("select id, status, committed_at, selection from changeset order by id").fetchall()
    assert [r["status"] for r in rows] == ["planned", "planned"] and all(r["committed_at"] is None for r in rows)
    assert rows[0]["selection"]["cleanup"] == {"cluster": "gmail|old@system.example|", "account": "gmail",
                                               "action": "archive"}
    ops = conn.execute("select message_id, op from changeset_op where changeset_id = %s", (made[1]["id"],)).fetchall()
    assert {o["message_id"] for o in ops} == set(ids["dead"][:10]) and {o["op"] for o in ops} == {"archive"}
    assert clusters.prepare_cleanup(conn, "gmail|old@system.example|", "archive")["changesets"] == []
    # a planned place under Talos/Automated/ is the label a message gets
    conn.execute("insert into structure_plan (message_id, account_id, place, target, rule_id, in_inbox, leaves_inbox,"
                 " rules_version, rules_sha) select id, 'gmail', 'label', 'Talos/Automated/Alerts & reports', 'auto',"
                 " false, false, 1, 'x' from unnest(%s::bigint[]) id", (ids["busy"],))
    busy = clusters.prepare_cleanup(conn, "gmail|b@busy.example|", "archive")["changesets"]
    assert busy[0]["args"] == {"label": "Talos/Automated/Alerts & reports"} and busy[0]["will_change"] == 30
    trash = clusters.prepare_cleanup(conn, "gmail|robot@x.example|[Success]", "trash")
    assert [(c["op"], c["will_change"]) for c in trash["changesets"]] == [("trash", 21)]
    assert "30 days" in trash["note"]
    assert conn.execute("select count(*) as n from changeset where status <> 'planned'").fetchone()["n"] == 0
    with pytest.raises(clusters.ClusterError, match="archive or trash"):
        clusters.prepare_cleanup(conn, "gmail|old@system.example|", "delete")
    with pytest.raises(changesets.ChangesetError):
        changesets.apply(conn, made[0]["id"], {})      # planned, never committed: nothing can apply it


def test_the_work_account_is_refused_until_mail_readwrite_and_no_changeset_is_made(conn, ingestor, vault, database):
    _mailbox(conn, ingestor)
    with pytest.raises(clusters.Refused, match="planned only") as exc:
        clusters.prepare_cleanup(conn, "work|firewall@company.example|", "archive")
    assert exc.value.would == {"account": "work", "action": "archive", "messages": 22, "on_server": 22, "in_inbox": 0}
    c = client(database, vault)
    r = c.post("/api/clusters/cleanup", headers=POST, json={"key": "work|firewall@company.example|", "action": "trash"})
    assert r.status_code == 409 and r.json()["refused"] and "planned only" in r.json()["error"]
    assert c.post("/api/clusters/cleanup", json={"key": "gmail|old@system.example|", "action": "trash"}).status_code == 403
    assert not conn.execute("select 1 from changeset").fetchone()
    r = c.post("/api/clusters/cleanup", headers=POST, json={"key": "gmail|old@system.example|", "action": "trash"})
    assert r.status_code == 201 and r.json()["changesets"][0]["status"] == "planned"


def test_the_clusters_are_computed_fast_at_fixture_scale_and_read_from_the_cache_after(
        conn, vault, database, monkeypatch):
    bulkmail.fill(conn, 20_000)
    conn.execute("update message set is_automated = true")
    conn.commit()
    t0 = time.monotonic()
    p = clusters.compute(conn)
    took = time.monotonic() - t0
    assert p["machine"] == 20_000 and took < 8, took
    assert sum(c["count"] for c in p["clusters"]) + p["small"]["messages"] == 20_000
    c = client(database, vault)
    first = c.get("/api/clusters").json()
    monkeypatch.setattr(clusters, "compute", lambda *a, **k: pytest.fail("computed again on a fresh load"))
    monkeypatch.setattr(clusters, "fingerprint", lambda c: first_fp)
    first_fp = conn.execute("select fingerprint from insight_cache where name = 'clusters'").fetchone()["fingerprint"]
    t0 = time.monotonic()
    again = c.get("/api/clusters").json()
    assert time.monotonic() - t0 < 1.0 and len(again["clusters"]) == len(first["clusters"])
