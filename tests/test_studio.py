"""The Studio (talos.studio) and the levels of sureness (talos.sureness): cards of grouped mail,
the owner's decisions and their undo, the calibration of Jev's cells and the lifts. Invented mail only."""

import json

import pytest
from test_enrich import ME, mail
from test_web import client

from talos import boundary, enrich, search, studio, sureness

pytestmark = pytest.mark.usefixtures("taxonomy_loaded")
HDR = {"X-Talos": "1"}


def _put(conn, mid, dim, value, *, status="proposed", conf=None, kind="model", scores=None):
    ev = {"scores": scores} if scores else {}
    return conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, status,"
                        " confidence, evidence) values (%s, %s, %s, %s, 'jev-test', %s, %s, %s) returning id",
                        (mid, dim, value, kind, status, conf, json.dumps(ev))).fetchone()["id"]


def _machine(conn, ingestor, frm, subject, n, *, days_ago=10):
    ids = [mail(ingestor, frm=frm, subject=f"{subject} {100 + i}", body=f"Larm {i}",
                headers={"Auto-Submitted": "auto-generated"}, days_ago=days_ago - i) for i in range(n)]
    for i in ids:
        _put(conn, i, "sender_kind", "machine", status="active", conf=0.99)
    return ids


def _archive(conn, ingestor):
    """Two systems' alarms (a group of 6 and one of 3) with Jev unsure of kind and value, and a
    thread with Pia where Jev is unsure of kind."""
    ids = {"alarms": _machine(conn, ingestor, "Monitor <monitor@example.org>", "Alarm on host", 6),
           "backups": _machine(conn, ingestor, "Backup <backup@example.org>", "Backup finished job", 3)}
    for i in ids["alarms"]:
        _put(conn, i, "kind", "alert", conf=0.62, scores={"alert": 0.62, "report": 0.3, "fyi": 0.08})
        _put(conn, i, "value", "transient", conf=0.66)
        _put(conn, i, "topic", "Work/Monitoring", status="active", conf=0.9)
    for i in ids["backups"]:
        _put(conn, i, "kind", "report", conf=0.74)
    ids["pia"] = [mail(ingestor, frm="Pia Ek <pia@example.org>", subject="Offert", body=f"Pia skriver {i}",
                       thread="pia", days_ago=5 - i) for i in range(2)]
    ids["owners"] = mail(ingestor, frm=ME, to="pia@example.org", subject="Re: Offert", body="Tack", thread="pia", days_ago=2)
    for i in ids["pia"]:
        _put(conn, i, "sender_kind", "people", status="active", conf=0.99)
        _put(conn, i, "kind", "question_request", conf=0.55)
    enrich.patterns(conn)
    conn.commit()
    return ids


def _eff(conn, mid, dim):
    r = conn.execute("select value, source_kind from effective_message_assignment where message_id = %s"
                     " and dimension_id = %s", (mid, dim)).fetchone()
    return (r["value"], r["source_kind"]) if r else None


# ---------------------------------------------------------------- levels

@pytest.mark.parametrize("source, conf, level", [
    ("human", None, "fact"), ("human", 0.2, "fact"), ("rule", None, "certain"), ("model", 0.97, "certain"),
    ("model", 0.9, "confident"), ("model", 0.85, "confident"), ("model", 0.72, "likely"), ("model", 0.5, "maybe"),
    ("model", 0.31, "doubtful"), ("model", 0.1, "guess"), ("model", None, "likely"), ("import", None, "likely")])
def test_a_values_level_names_its_span_of_confidence_and_the_owners_own_is_always_fact(conn, source, conf, level):
    assert sureness.level_of(source, conf) == level
    got = conn.execute(f"select {sureness.level_sql('%(s)s::text', '%(c)s::real')} as l",
                       {"s": source, "c": conf}).fetchone()["l"]
    assert got == level                                          # the SQL agrees with the Python


def test_the_levels_are_ordered_from_fact_down_and_a_floor_says_what_is_as_sure_or_surer():
    assert [x["key"] for x in sureness.levels()] == ["fact", "certain", "confident", "likely", "maybe", "doubtful",
                                                     "guess"]
    assert sureness.at_least("certain", "likely") and not sureness.at_least("maybe", "likely")
    with pytest.raises(sureness.SurenessError):
        sureness.check("sure-ish")


def test_the_lift_needs_enough_agreeing_verdicts_for_a_lower_bound_of_confident():
    assert studio.wilson_lower(0, 0) == 0.0
    assert studio.need(0, 0) == 16                               # sixteen yes in a row lift a new cell
    assert studio.wilson_lower(16, 16) >= 0.85 > studio.wilson_lower(15, 15)
    assert studio.need(10, 14) is None                           # four wrong in fourteen: never within reach


# ---------------------------------------------------------------- the pool and the cards

def test_the_pool_groups_machine_mail_by_pattern_and_people_by_thread_ranked_by_what_one_card_settles(
        conn, ingestor):
    ids = _archive(conn, ingestor)
    p = studio.compute_pool(conn)
    keys = [g["key"] for g in p["groups"]]
    alarms = next(g for g in p["groups"] if g["rep"] in ids["alarms"])
    assert keys[0] == alarms["key"] and alarms["key"].startswith("p:monitor@example.org|")
    assert alarms["messages"] == 6 and alarms["unsettled"] == {"sender_kind": 0, "kind": 6, "topic": 0, "value": 6}
    pia = next(g for g in p["groups"] if g["rep"] in ids["pia"])
    assert pia["key"] == f"t:{conn.execute('select thread_id from message where id = %s', (ids['pia'][0],)).fetchone()['thread_id']}"
    assert pia["messages"] == 2                                  # the owner's own reply is not in the group
    cells = {(c["field"], c["value"], c["level"], c["side"]): c["messages"] for c in p["cells"]} if p["cells"] else {}
    assert cells == {}                                           # too small to be worth a check (CELL_MIN)


def test_a_card_shows_each_quality_with_its_level_and_jevs_alternatives(conn, ingestor):
    ids = _archive(conn, ingestor)
    key = studio.group_of(conn, ids["alarms"][0])
    c = studio.card(conn, key)
    assert c["messages"] == 6 and c["machine"] and c["group"] == "pattern" and c["rep"]["id"] == ids["alarms"][-1]
    lines = {ln["field"]: ln for ln in c["lines"]}
    assert (lines["kind"]["value"], lines["kind"]["level"], lines["kind"]["status"]) == ("alert", "maybe", "proposed")
    assert lines["kind"]["alternatives"][:2] == ["report", "fyi"]
    assert lines["topic"]["level"] == "confident" and lines["topic"]["done"]
    assert lines["sender_kind"]["done"] and not lines["kind"]["done"]


# ---------------------------------------------------------------- deciding

def test_a_decision_writes_the_owners_values_on_the_whole_group_with_their_sides_and_leaves_the_feed(conn, ingestor):
    ids = _archive(conn, ingestor)
    key = studio.group_of(conn, ids["alarms"][0])
    res = studio.decide(conn, key, [{"field": "kind", "value": "alert"}, {"field": "value", "value": "transient"}])
    conn.commit()
    assert res["settled"] == 6 and res["messages"] == 6
    for i in ids["alarms"]:
        assert _eff(conn, i, "kind") == ("alert", "human") and _eff(conn, i, "value") == ("transient", "human")
    keep = boundary.sides(conn)["keep"]["side_of"]["transient"]
    assert _eff(conn, ids["alarms"][2], "keep") == (keep, "human")   # the side follows, as the owner's
    v = conn.execute("select field, value, level, side, agreed, chosen from studio_verdict order by field").fetchall()
    assert [(x["field"], x["level"], x["side"], x["agreed"]) for x in v] == [("kind", "maybe", "machine", True),
                                                                             ("value", "maybe", "machine", True)]
    studio.pool(conn, refresh=True)
    assert key not in [c["key"] for c in studio.feed(conn, n=5)["cards"]]


def test_no_rejects_jevs_value_without_writing_one_and_one_writes_on_the_message_alone(conn, ingestor):
    ids = _archive(conn, ingestor)
    key = studio.group_of(conn, ids["alarms"][0])
    res = studio.decide(conn, key, [{"field": "kind", "value": None}], scope="one", rep_id=ids["alarms"][3])
    assert res["settled"] == 0 and res["messages"] == 1
    st = {r["entity_id"]: r["status"] for r in conn.execute(
        "select entity_id, status from assignment where dimension_id = 'kind' and entity_id = any(%s)", (ids["alarms"],))}
    assert st[ids["alarms"][3]] == "rejected" and st[ids["alarms"][0]] == "proposed"
    assert conn.execute("select agreed, chosen from studio_verdict").fetchone() == {"agreed": False, "chosen": None}
    studio.decide(conn, key, [{"field": "kind", "value": "report"}], scope="one", rep_id=ids["alarms"][1])
    assert _eff(conn, ids["alarms"][1], "kind") == ("report", "human") and _eff(conn, ids["alarms"][0], "kind") is None


def test_undo_takes_a_decision_back_and_restores_what_it_replaced(conn, ingestor):
    ids = _archive(conn, ingestor)
    key = studio.group_of(conn, ids["alarms"][0])
    studio.decide(conn, key, [{"field": "kind", "value": "report"}])
    second = studio.decide(conn, key, [{"field": "kind", "value": "alert"}, {"field": "value", "value": None}])
    assert _eff(conn, ids["alarms"][0], "kind") == ("alert", "human")
    res = studio.undo(conn)
    assert res["decision"] == second["decision"] and res["restored"] == 6 and res["reopened"] == 6
    assert _eff(conn, ids["alarms"][0], "kind") == ("report", "human")      # the earlier decision is back
    assert conn.execute("select count(*) as n from assignment where dimension_id = 'value' and status = 'proposed'"
                        " and entity_id = any(%s)", (ids["alarms"],)).fetchone()["n"] == 6
    studio.undo(conn)
    assert _eff(conn, ids["alarms"][0], "kind") is None
    with pytest.raises(studio.StudioError, match="nothing to undo"):
        studio.undo(conn)


def test_a_decision_refuses_a_value_outside_the_taxonomy_and_an_unknown_field(conn, ingestor):
    ids = _archive(conn, ingestor)
    key = studio.group_of(conn, ids["alarms"][0])
    with pytest.raises(studio.StudioError, match="not a value of kind"):
        studio.decide(conn, key, [{"field": "kind", "value": "banana"}])
    with pytest.raises(studio.StudioError, match="a field is one of"):
        studio.decide(conn, key, [{"field": "route", "value": "x"}])
    with pytest.raises(studio.StudioError, match="not a group key"):
        studio.group_ids(conn, "z:1")


# ---------------------------------------------------------------- calibration and lifts

def _cell_mail(conn, ingestor, n, *, value="alert", conf=0.62, frm="sys{i}@example.org"):
    """n single messages from different systems, each with Jev's kind proposal in one cell."""
    out = []
    for i in range(n):
        mid = _machine(conn, ingestor, f"System <{frm.format(i=i)}>", f"Notice number {i}", 1)[0]
        _put(conn, mid, "kind", value, conf=conf)
        out.append(mid)
    enrich.patterns(conn)
    return out


def test_enough_agreeing_verdicts_lift_the_cells_other_proposals_and_the_lift_can_be_undone(conn, ingestor):
    judged = _cell_mail(conn, ingestor, 16)
    rest = _cell_mail(conn, ingestor, 5, frm="other{i}@example.org")
    other_cell = _cell_mail(conn, ingestor, 2, conf=0.75, frm="likely{i}@example.org")   # Likely: another cell
    lifts = []
    for mid in judged:
        lifts += studio.decide(conn, studio.group_of(conn, mid), [{"field": "kind", "value": "alert"}])["lifts"]
    assert len(lifts) == 1 and lifts[0]["verdicts"] == 16 and lifts[0]["messages"] == 5
    assert lifts[0]["calibrated"] >= studio.LIFT_FLOOR
    for mid in rest:
        r = conn.execute("select status, confidence, decided_by, evidence from assignment where entity_id = %s"
                         " and dimension_id = 'kind'", (mid,)).fetchone()
        assert r["status"] == "active" and r["decided_by"] == f"studio-lift:{lifts[0]['lift']}"
        assert r["confidence"] == pytest.approx(lifts[0]["calibrated"], abs=1e-4)
        assert r["evidence"]["jev_confidence"] == pytest.approx(0.62)
        assert sureness.level_of("model", r["confidence"]) == "confident"
    assert all(_eff(conn, m, "kind") is None for m in other_cell)
    # new mail in the cell: relift takes it
    new = _cell_mail(conn, ingestor, 1, frm="late{i}@example.org")
    assert studio.relift(conn)["messages"] == 1 and _eff(conn, new[0], "kind") == ("alert", "model")
    s = studio.stats(conn)
    assert s["all"]["lifted"] == 6 and s["all"]["settled"] == 16 and s["all"]["total"] == 22
    back = studio.undo_lift(conn, lifts[0]["lift"])
    assert back["returned"] == 5
    r = conn.execute("select status, confidence from assignment where entity_id = %s and dimension_id = 'kind'",
                     (rest[0],)).fetchone()
    assert r["status"] == "proposed" and r["confidence"] == pytest.approx(0.62)


def test_disagreeing_verdicts_keep_a_cell_from_lifting_and_the_calibration_says_so(conn, ingestor):
    judged = _cell_mail(conn, ingestor, 16)
    _cell_mail(conn, ingestor, 3, frm="rest{i}@example.org")
    for n, mid in enumerate(judged):
        studio.decide(conn, studio.group_of(conn, mid), [{"field": "kind", "value": "alert" if n % 3 else "report"}])
    assert conn.execute("select count(*) as n from studio_lift").fetchone()["n"] == 0
    (cal,) = [c for c in studio.calibration(conn) if c["field"] == "kind"]
    assert (cal["n"], cal["a"], cal["level"], cal["side"]) == (16, 10, "maybe", "machine") and cal["need"] is None


def test_a_check_card_samples_the_cell_closest_to_a_lift_from_a_sender_it_has_not_seen(conn, ingestor, monkeypatch):
    monkeypatch.setattr(studio, "CELL_MIN", 3)
    judged = _cell_mail(conn, ingestor, 4)
    studio.decide(conn, studio.group_of(conn, judged[0]), [{"field": "kind", "value": "alert"}])
    p = studio.compute_pool(conn)
    cells = studio.check_cells(conn, p)
    top = cells[0]
    assert (top["field"], top["value"], top["level"], top["side"]) == ("kind", "alert", "maybe", "machine")
    assert top["verdicts"] == 1 and top["need"] == 15 and top["senders"] == ["sys0@example.org"]
    c = studio._check_card(conn, top, set(), set())
    assert c["kind"] == "check" and c["rep"]["id"] in judged[1:] and c["cell"]["need"] == 15


# ---------------------------------------------------------------- the web

def test_the_studio_routes_feed_decide_and_undo_and_writes_need_the_header(conn, ingestor, vault, database):
    ids = _archive(conn, ingestor)
    studio.pool(conn, refresh=True)
    conn.commit()
    c = client(database, vault)
    feed = c.get("/api/studio/next?n=2").json()
    assert feed["cards"][0]["messages"] == 6 and {x["key"] for x in feed["levels"]} >= {"fact", "maybe"}
    assert "alert" in {o["value"] for o in feed["options"]["kind"]}
    card = feed["cards"][0]
    body = {"action": "decide", "key": card["key"], "rep_id": card["rep"]["id"],
            "lines": [{"field": "kind", "value": "alert"}]}
    assert c.post("/api/studio", json=body).status_code == 403
    res = c.post("/api/studio", json=body, headers=HDR).json()
    assert res["settled"] == 6
    stats = c.get("/api/studio/stats?since=2020-01-01T00:00:00Z").json()
    assert stats["session"]["settled"] == 6 and stats["recent"][0]["id"] == res["decision"]
    assert c.post("/api/studio", json={"action": "undo"}, headers=HDR).json()["decision"] == res["decision"]
    assert c.post("/api/studio", json={"action": "fly"}, headers=HDR).status_code == 400
    assert _eff(conn, ids["alarms"][0], "kind") is None


# ---------------------------------------------------------------- sureness in Mail

def test_the_sureness_filter_keeps_values_as_sure_as_asked_and_reaches_into_jevs_guesses(conn, ingestor):
    ids = _archive(conn, ingestor)
    studio.decide(conn, studio.group_of(conn, ids["backups"][0]), [{"field": "kind", "value": "report"}],
                  scope="one", rep_id=ids["backups"][0])
    conn.commit()
    got = lambda **kw: {r["id"] for r in search.messages(conn, dimension=[("kind", kw.pop("v"))], limit=50, **kw)["rows"]}
    assert got(v="report") == {ids["backups"][0]}                         # as decided: the owner's value only
    assert got(v="report", sure="likely") == set(ids["backups"])          # Jev's 0.74 guesses come in
    assert got(v="report", sure="confident") == {ids["backups"][0]}
    assert got(v="report", sure="fact") == {ids["backups"][0]}
    assert got(v="alert", sure="maybe") == set(ids["alarms"])
    assert got(v="alert", sure="likely") == set()
    assert got(v="question_request", sure="maybe") == set(ids["pia"])
    with pytest.raises(sureness.SurenessError):
        search.messages(conn, dimension=[("kind", "alert")], sure="kinda")


# ---------------------------------------------------------------- mail that has left the mailbox

def _work(ingestor, folder, key, subject="Alarm", **loc):
    import mailfactory as mf
    from talos.ingest import Location
    return ingestor.ingest("work", mf.make(subject=subject, msgid=f"<{key}@test.invalid>"),
                           Location(folder, key, **loc)).message_id


def _trash_op(conn, mid, status="done"):
    cs = conn.execute("insert into changeset (title, status) values ('Prune: test', 'done') returning id").fetchone()["id"]
    conn.execute("insert into changeset_op (changeset_id, message_id, account_id, op, status, applied_at)"
                 " values (%s, %s, 'work', 'trash', %s, now())", (cs, mid, status))
    return cs


def test_a_message_in_the_trash_is_purged_when_talos_moved_it_deleted_otherwise_and_unmarked_once_restored(
        conn, ingestor):
    purged = _work(ingestor, "id-del", "p1", folder_path="Borttagna objekt")
    deleted = _work(ingestor, "id-del", "d1", folder_path="Borttagna objekt")
    restored = _work(ingestor, "id-inbox", "r1", folder_path="Inkorgen")
    gone = _work(ingestor, "id-inbox", "g1", folder_path="Inkorgen")
    live = _work(ingestor, "id-inbox", "l1", folder_path="Inkorgen")
    cs = _trash_op(conn, purged)
    _trash_op(conn, restored)                                      # trashed once, then moved back
    _trash_op(conn, deleted, status="failed")                      # a failed op moved nothing
    conn.execute("update message_location set present = false where message_id = %s", (gone,))
    conn.commit()
    rows = {r["id"]: r["removed"] for r in search.messages(conn, account="work", limit=50)["rows"]}
    assert rows == {purged: "purged", deleted: "deleted", restored: None, gone: "gone", live: None}
    m = search.message(conn, purged)
    assert m["removed"] == "purged" and m["purged_by"]["changeset_id"] == cs and m["purged_by"]["title"] == "Prune: test"
    assert "purged_by" not in search.message(conn, deleted)


def test_the_pool_ranks_mail_still_in_the_owners_folders_before_a_bigger_group_already_purged(conn, ingestor):
    ids = _archive(conn, ingestor)
    conn.execute("update message_location set folder = 'Trash', folder_path = 'Trash' where message_id = any(%s)",
                 (ids["alarms"],))                                  # the bigger group: all in the trash
    conn.commit()
    p = studio.compute_pool(conn)
    first = p["groups"][0]
    assert first["key"] == studio.group_of(conn, ids["backups"][0]) and first["live"] == 3
    alarms = next(g for g in p["groups"] if g["rep"] in ids["alarms"])
    assert alarms["live"] == 0 and alarms["score"] > first["score"]    # more to settle, but shown after
    c = studio.card(conn, alarms["key"])
    assert c["live"] == 0 and c["rep"]["removed"] == "deleted"


def test_a_decision_through_the_web_replans_its_messages_after_it_is_committed(conn, ingestor, vault, database,
                                                                               monkeypatch):
    ids = _archive(conn, ingestor)
    seen = []

    def replan(c, decision_id):
        # the decision is committed before the planner runs: another connection sees it
        with __import__("talos.db", fromlist=["db"]).connect(database) as other:
            seen.append(other.execute("select count(*) as n from studio_decision where id = %s",
                                      (decision_id,)).fetchone()["n"])
        return None
    monkeypatch.setattr(studio, "replan", replan)
    c = client(database, vault)
    key = studio.group_of(conn, ids["alarms"][0])
    res = c.post("/api/studio", json={"action": "decide", "key": key, "lines": [{"field": "kind", "value": "alert"}]},
                 headers=HDR).json()
    assert seen == [1]
    c.post("/api/studio", json={"action": "skip", "key": studio.group_of(conn, ids["backups"][0])}, headers=HDR)
    assert seen == [1]                                             # a skip changes no value: no replan
    c.post("/api/studio", json={"action": "undo", "decision": res["decision"]}, headers=HDR)
    assert seen == [1, 1]
