"""Rule suggestions (talos.suggest): drafted from accepted values, grouped by category, always off."""

from datetime import datetime, timedelta, timezone

import mailfactory as mf
import pytest
from starlette.testclient import TestClient

from talos import rules, suggest
from talos.config import Settings
from talos.ingest import Location
from talos.web import app

H = {"X-Talos": "1"}
T0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
N = {"uid": 0}


def mails(conn, ingestor, frm, subjects, **values):
    """One message per subject from frm, each with these values accepted from Jev (a model value, active)."""
    ids = []
    for s in subjects:
        N["uid"] += 1
        at = T0 + timedelta(hours=N["uid"])
        mid = ingestor.ingest("gmail", mf.make(frm=frm, subject=s, date=at),
                              Location("[all]", f"s{N['uid']}", uidvalidity=1, uid=N["uid"], received_at=at)).message_id
        for dim, value in values.items():
            conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, status)"
                         " values (%s, %s, %s, 'model', 'run-1', 'active')", (mid, dim, value))
        ids.append(mid)
    conn.commit()
    return ids


def by_sender(rows):
    return {(r["sender"], r["skeleton"], r["dimension"]): r for r in rows}


@pytest.fixture
def taxonomy(conn, taxonomy_loaded):
    """The taxonomy loaded; afterwards the values are removed first, so the taxonomy can drop a field
    (kind) the migrations may not have left in this database."""
    yield
    conn.rollback()
    conn.execute("delete from assignment")
    conn.commit()


@pytest.fixture
def archive(conn, ingestor, taxonomy):
    mails(conn, ingestor, "Monitor <alert@monitor.example>", [f"Disk {i} full" for i in range(20)],
          kind="alert", topic="Work/Monitoring")
    mails(conn, ingestor, "Almost <few@example.com>", [f"Note {i}" for i in range(19)], kind="alert")
    # A sender that conflicts as a whole, but one of its subject patterns agrees on its own.
    mails(conn, ingestor, "Backup <backup@nas.example>", [f"SV: [OK] Backup report {i}" for i in range(21)], kind="report")
    mails(conn, ingestor, "Backup <backup@nas.example>", ["Weekly news", "Weekly news again"], kind="report")
    mails(conn, ingestor, "Backup <backup@nas.example>", ["A question for you"], kind="fyi")
    # A value set by a rule is covered already: it does not count as accepted.
    ruled = mails(conn, ingestor, "Shop <shop@store.example>", [f"Order {i}" for i in range(20)])
    for mid in ruled:
        conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, status)"
                     " values (%s, 'kind', 'transaction', 'rule', 'rule:shop@1', 'active')", (mid,))
    conn.commit()


def test_a_sender_needs_twenty_agreeing_messages_and_no_conflict(conn, archive):
    got = by_sender(suggest.draft(conn))
    assert ("alert@monitor.example", None, "kind") in got
    assert not any(k[0] == "few@example.com" for k in got)            # 19 is below the threshold
    assert ("backup@nas.example", None, "kind") not in got             # a conflicting fyi
    assert not any(k[0] == "shop@store.example" for k in got)          # rule-made values are not accepted ones
    s = got[("alert@monitor.example", None, "kind")]
    assert s["agree"] == 20 and s["value"] == "alert" and s["enabled"] is False
    assert s["conditions"] == [{"field": "from_address", "op": "is", "value": "alert@monitor.example"}]
    assert s["action"] == {"dimension": "kind", "value": "alert"}
    assert len(suggest.draft(conn, min_messages=19)) > len(suggest.draft(conn))


def test_a_conflicting_sender_gets_a_rule_for_the_subject_pattern_that_agrees(conn, archive):
    got = by_sender(suggest.draft(conn))
    s = got[("backup@nas.example", "ok backup report", "kind")]
    assert s["agree"] == 21 and s["value"] == "report"
    subj = s["conditions"][1]
    assert subj["field"] == "subject" and subj["op"] == "matches"
    detail = suggest.details(conn, [s])[0]
    assert detail["preview"] == 21                                    # not the weekly news, nor the question
    assert len(detail["examples"]) == 3 and all("Backup report" in e["subject"] for e in detail["examples"])
    assert rules.preview(conn, s["conditions"])["count"] == 21        # the saved rule would match the same


def test_the_subject_regex_matches_exactly_the_messages_of_its_skeleton(conn, ingestor):
    subjects = ["Re: SV: Faktura 2026-09 från Telia", "Faktura 2026-10, 11 från Telia!", "faktura #12 från telia ab",
                "Faktura från Telia", "Fakturan 2026 från Telia", "sv faktura 1 från telia", "Faktura - 2026 - från Telia"]
    for i, s in enumerate(subjects):
        ingestor.ingest("gmail", mf.make(frm="Telia <faktura@telia.se>", subject=s),
                        Location("[all]", f"f{i}", uidvalidity=1, uid=900 + i))
    conn.commit()
    for sk in {r["skeleton"] for r in conn.execute("select skeleton from message_pattern")}:
        rx = suggest.skeleton_regex(sk)
        rows = conn.execute("select m.subject, mp.skeleton, coalesce(m.subject, '') ~* %s as hit from message m"
                            " join message_pattern mp on mp.message_id = m.id", (rx,)).fetchall()
        assert all(r["hit"] == (r["skeleton"] == sk) for r in rows), (sk, rows)


def test_suggestions_are_grouped_by_value_or_by_family(conn, archive):
    cats = {c["key"]: c for c in suggest.categories(suggest.draft(conn))}
    assert cats["kind:alert"]["name"] == "Alert" and cats["kind:alert"]["count"] == 1   # few values: by value
    assert cats["kind:report"]["name"] == "Report"
    assert cats["topic:Work"]["name"] == "Work" and cats["topic:Work"]["dimension_label"] == "Topic"  # by family
    order = [c["dimension"] for c in suggest.categories(suggest.draft(conn))]
    assert order == sorted(order, key=suggest.DIMS.index)


def test_his_own_address_is_never_a_sender_to_suggest_for(conn, ingestor, taxonomy):
    mails(conn, ingestor, "Alex <owner@gmail.com>", [f"Notes {i}" for i in range(25)], kind="fyi")
    assert suggest.draft(conn) == []


def test_adding_a_suggestion_saves_it_off_and_nothing_is_created_before(conn, archive, vault, database):
    assert conn.execute("select count(*) as n from rule").fetchone()["n"] == 0
    c = TestClient(app.create(Settings(home=vault.root.parent, dsn=database), allowed_hosts=["testserver"]))
    res = c.get("/api/rules/suggestions").json()
    assert res["threshold"] == 20 and res["total"] >= 3
    keys = {x["key"] for x in res["categories"]}
    assert {"kind:alert", "kind:report", "topic:Work"} <= keys
    assert conn.execute("select count(*) as n from rule").fetchone()["n"] == 0      # listing creates nothing
    page = c.get("/api/rules/suggestions", params={"category": "kind:alert"}).json()
    [row] = page["rows"]
    assert row["preview"] == 20 and len(row["examples"]) == 3 and row["enabled"] is False

    assert c.post("/api/rules/suggestions/add", json={"id": row["id"]}).status_code == 403
    assert c.post("/api/rules/suggestions/add", json={"id": "suggest-nope"}, headers=H).status_code == 404
    r = c.post("/api/rules/suggestions/add", json={"id": row["id"]}, headers=H)
    assert r.status_code == 201
    saved = r.json()["rule"]
    assert saved["enabled"] is False and saved["action"] == {"dimension": "kind", "value": "alert"}
    assert saved["priority"] == suggest.PRIORITY and "Suggested by Talos" in saved["description"]
    assert c.post("/api/rules/suggestions/add", json={"id": row["id"]}, headers=H).status_code == 409
    # It is a rule now, so it is no longer suggested; and being off, a rule run sets nothing with it.
    again = c.get("/api/rules/suggestions", params={"category": "kind:alert"}).json()
    assert again["count"] == 0
    rules.run_all(conn)
    conn.commit()
    assert conn.execute("select count(*) as n from assignment where source_ref like 'rule:suggest-%%'").fetchone()["n"] == 0
