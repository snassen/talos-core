"""Unlocking the labelling (talos.unlock): the low-hanging fruit, its answer key, Claude's export,
giving the answers to the groups, and the improvement jobs. Invented mail only; never the real Jev."""

import json
import time

import bulkmail
import pytest
from test_accept import _accepting, _gold_run
from test_enrich import mail
from test_gold import POST
from test_jev import keychain  # noqa: F401  (keychain is a fixture)
from test_web import client

from talos import focus, gold, insight, jev, personal, unlock

AUTO = {"Auto-Submitted": "auto-generated"}
ROBOT = "IT Robot <itrobot@company.example>"
WORDS = ["alfa", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]
ANSWER = {"origin": ["notification"], "kind": ["report"], "topic": ["Work/Backup"], "value": ["transient"]}


@pytest.fixture(autouse=True)
def kind_exists(conn):
    """Migration 019 makes the kind dimension; test_structure's fixture deletes it after its tests."""
    conn.execute("insert into dimension (id, label, cardinality) values ('kind', 'Kind', 'one') on conflict do nothing")
    conn.commit()


def _val(conn, mids, dim, value, source="model"):
    for m in mids:
        conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, status)"
                     " values (%s, %s, %s, %s, 'test', 'active')", (m, dim, value, source))


def _undecided(conn, ingestor) -> dict:
    """itrobot@ (shared: eight [Success], six [Failed], two QNAP), a newsletter of seven subjects, a
    friend (people: left out), a sender decided on every key field (left out) and one missing kind only."""
    ids: dict = {}
    ids["success"] = [mail(ingestor, frm=ROBOT, subject=f"[Success] Job {WORDS[i]}", headers=AUTO, days_ago=30 - i)
                      for i in range(8)]
    ids["failed"] = [mail(ingestor, frm=ROBOT, subject=f"[Failed] Job {WORDS[i]}", headers=AUTO, days_ago=60 - i)
                     for i in range(6)]
    ids["qnap"] = [mail(ingestor, frm=ROBOT, subject=f"QNAP - disk {i}", headers=AUTO, days_ago=70 - i) for i in range(2)]
    ids["news"] = [mail(ingestor, frm="Butiken <news@butiken.example>", subject=s, days_ago=80 - i)
                   for i, s in enumerate(["Höstrea", "Nya jackor", "Sista chansen", "Veckans tips", "Julklappar",
                                          "Fri frakt", "Medlemsdagar"])]
    ids["friend"] = [mail(ingestor, frm="Vän <van@example.org>", subject=f"Hej {i}", days_ago=5 + i) for i in range(9)]
    _val(conn, ids["friend"], "sender_kind", "people")
    ids["done"] = [mail(ingestor, frm="Klar <klar@example.se>", subject=f"Kvitto {i}", headers=AUTO, days_ago=3 + i)
                   for i in range(6)]
    ids["kind"] = [mail(ingestor, frm="Kind <kind@example.se>", subject=f"Rapport {i}", headers=AUTO, days_ago=2 + i)
                   for i in range(5)]
    for key in ("done", "kind"):
        _val(conn, ids[key], "topic", "Work/Backup")
        _val(conn, ids[key], "value", "transient")
        _val(conn, ids[key], "sender_kind", "machine")
        _val(conn, ids[key], "keep", "short_lived")
    _val(conn, ids["done"], "kind", "report")
    conn.commit()
    return ids


# ---------------------------------------------------------------- the low-hanging fruit

def test_the_fruit_ranks_sender_groups_of_undecided_machine_mail_by_the_messages_one_answer_unlocks(conn, ingestor):
    ids = _undecided(conn, ingestor)
    # a message already in an answer key is never counted again
    conn.execute("insert into gold_set (id, name, seed, target) values (1, 'k', 1, 1)")
    conn.execute("insert into gold_item (set_id, position, message_id, stratum, reason, unit)"
                 " values (1, 1, %s, 'machine', 'r', 'message')", (ids["news"][0],))
    conn.commit()
    g = unlock.groups(conn)["groups"]
    assert [(x["sender"], x["system"], x["messages"]) for x in g] == [
        ("itrobot@company.example", "[Success]", 8), ("itrobot@company.example", "[Failed]", 6),
        ("news@butiken.example", None, 6), ("kind@example.se", None, 5)]          # QNAP: two, no group
    top = g[0]
    assert top["missing"] == {f: 8 for f in unlock.KEY_FIELDS}
    assert top["fields"] == ["origin", "kind", "topic", "value"]        # sender_kind is asked as origin, keep as value
    assert g[3]["missing"] == {"kind": 5} and g[3]["fields"] == ["kind"]
    assert top["rep"] == ids["success"][-1] and [e["subject"] for e in top["examples"]] == [
        "[Success] Job hotel", "[Success] Job golf"]                   # no patterns yet: the newest first
    assert not {m for x in g for m in x["ids"]} & set(ids["friend"] + ids["done"] + [ids["news"][0]])
    p = unlock.compute_fruit(conn)
    assert p["undecided"] == 8 + 6 + 2 + 6 + 5 and p["grouped"] == 25 and p["top_messages"] == 25
    assert "ids" not in p["top"][0]


def test_label_these_ten_makes_an_answer_key_of_the_representatives_that_asks_only_what_is_missing(
        conn, ingestor, vault, database):
    ids = _undecided(conn, ingestor)
    res = unlock.label_set(conn, top=2)
    conn.commit()
    sid = res["set_id"]
    assert res["items"] == 2 and res["messages"] == 15 and res["fields"] == ["origin", "kind", "topic", "value"]
    assert gold.set_fields(conn, sid) == ["origin", "kind", "topic", "value"]
    items = conn.execute("select * from gold_item where set_id = %s order by position", (sid,)).fetchall()
    assert [i["message_id"] for i in items] == [ids["success"][-1], ids["news"][-1]]
    assert [i["info"]["sender_group"]["key"] for i in items] == ["itrobot@company.example | [Success]",
                                                                 "news@butiken.example"]
    frozen = {r["item_id"]: r["n"] for r in conn.execute(
        "select item_id, count(*)::int as n from gold_group_message group by 1")}
    assert frozen == {items[0]["id"]: 8, items[1]["id"]: 7}
    # the blind screen shows the group: its size and a few subjects, never a value
    it = client(database, vault).get(f"/api/gold/{sid}/items/1").json()
    assert it["group"]["messages"] == 8 and it["fields"] == ["origin", "kind", "topic", "value"]
    assert len(it["group"]["examples"]) == 8 and "assignment" not in json.dumps(it)
    # the next set never repeats a message of this one
    nxt = unlock.label_set(conn, top=10)
    assert [g["key"] for g in nxt["groups"]] == ["itrobot@company.example | [Failed]", "kind@example.se"]
    with pytest.raises(unlock.UnlockError, match="no sender group"):
        unlock.label_set(conn)


def test_apply_to_the_groups_counts_in_a_dry_run_then_gives_the_owners_answers_and_their_sides_to_every_message(
        conn, ingestor, taxonomy_loaded):
    ids = _undecided(conn, ingestor)
    sid = unlock.label_set(conn, top=2)["set_id"]
    conn.commit()
    gold.save_label(conn, sid, 1, "origin", ANSWER["origin"])
    with pytest.raises(unlock.UnlockError, match="not fully labelled"):
        unlock.apply(conn, sid)
    for pos in (1, 2):
        for f, v in ANSWER.items():
            gold.save_label(conn, sid, pos, f, v)
    conn.commit()
    dry = unlock.apply(conn, sid)
    assert dry["dry_run"] and dry["messages"] == 15
    assert dry["written"] == 4 * 15 and dry["sides"] == {"sender_kind": 15, "keep": 15}
    assert not conn.execute("select 1 from assignment where source_ref like 'gold:%'").fetchone()
    res = unlock.apply(conn, sid, dry_run=False)
    conn.commit()
    assert res["written"] == dry["written"] and res["sides_written"] == 30
    row = conn.execute("select value, source_kind, decided_by, evidence from assignment where entity_id = %s"
                       " and dimension_id = 'keep' and status = 'active'", (ids["success"][3],)).fetchone()
    assert (row["value"], row["source_kind"], row["decided_by"]) == ("short_lived", "human", personal.OWNER_ID)
    assert row["evidence"]["side_of"] == "value"
    assert conn.execute("select params->'applied'->>'labeller' as l from gold_set where id = %s",
                        (sid,)).fetchone()["l"] == personal.OWNER_ID
    # idempotent, and the groups are decided now: they are no longer fruit
    again = unlock.apply(conn, sid, dry_run=False)
    assert again["written"] == 0 and again["sides_written"] == 0
    assert {x["key"] for x in unlock.groups(conn)["groups"]} == {"itrobot@company.example | [Failed]", "kind@example.se"}
    with pytest.raises(unlock.UnlockError, match="not an unlock set"):
        conn.execute("insert into gold_set (id, name, seed, target) values (99, 'other', 99, 1)")
        unlock.apply(conn, 99)


def test_let_claude_label_writes_the_set_blind_to_a_file_with_the_import_command_and_calls_no_model(
        conn, ingestor, tmp_path, monkeypatch, taxonomy_loaded):
    def no_model(*a, **k):
        raise AssertionError("no model may be called")
    monkeypatch.setattr(jev, "JevClient", no_model)
    _undecided(conn, ingestor)
    sid = unlock.label_set(conn, top=2)["set_id"]
    out = unlock.export_for_claude(conn, sid, tmp_path)
    conn.commit()
    with open(out["items"], encoding="utf-8") as fh:
        lines = [json.loads(x) for x in fh.read().splitlines()]
    assert out["count"] == 2 and [x["position"] for x in lines] == [1, 2]
    for x in lines:
        assert x["fields"] == ["origin", "kind", "topic", "value"] and x["group"]["messages"] in (8, 7)
        assert not {"labels", "round_items", "duration_ms"} & set(x)
        text = json.dumps(x)
        assert not [k for k in ('"is_automated"', '"importance"', '"stratum"', '"reason"', '"missing"') if k in text]
    with open(out["options"], encoding="utf-8") as fh:
        opts = json.load(fh)
    assert set(opts["options"]) == {"origin", "kind", "topic", "value"} and opts["answer_line"]["position"] == 1
    assert out["commands"]["import"] == (f"uv run talos enrich gold import-labels --set {sid} --labeller claude"
                                         f" {out['labels']}")
    # what the lead does later: Claude's answers in that file, imported, then given to the groups
    with open(out["labels"], "w", encoding="utf-8") as fh:
        for pos in (1, 2):
            fh.write(json.dumps({"position": pos, **{f: v[0] for f, v in ANSWER.items()}}) + "\n")
    gold.import_labels(conn, sid, "claude", [out["labels"]])
    conn.commit()
    dry = unlock.apply(conn, sid)
    assert dry["labeller"] == "claude" and dry["written"] == 4 * 15


def test_the_widget_endpoints_make_a_set_export_it_and_refuse_a_post_without_the_header(
        conn, ingestor, vault, database, taxonomy_loaded):
    _undecided(conn, ingestor)
    c = client(database, vault)
    fr = c.get("/api/unlock/fruit").json()
    assert [g["messages"] for g in fr["top"]] == [8, 7, 6, 5] and fr["sets"] == [] and fr["stale"] is False
    assert c.post("/api/unlock/sets", json={"top": 3}).status_code == 403
    r = c.post("/api/unlock/sets", headers=POST, json={"top": 3, "claude": True})
    assert r.status_code == 201
    body = r.json()
    assert body["items"] == 3 and body["export"]["items"].startswith(str(vault.root.parent / "exports"))
    fr = c.get("/api/unlock/fruit").json()          # making the set stored what is left: no second pass
    assert fr["stale"] is False and [s["id"] for s in fr["sets"]] == [body["set_id"]] and fr["sets"][0]["done"] == 0
    assert fr["sets"][0]["export"]["commands"]["import"].startswith("uv run talos enrich gold import-labels")
    assert [g["messages"] for g in fr["top"]] == [5]                   # what is in the set is not fruit again
    r = c.post(f"/api/unlock/sets/{body['set_id']}/apply", headers=POST, json={"dry_run": True})
    assert r.status_code == 409 and "not fully labelled" in r.json()["error"]


def test_the_fruit_is_fast_at_fixture_scale_and_a_fresh_widget_load_never_computes_it_again(
        conn, vault, database, monkeypatch):
    bulkmail.fill(conn, 20_000)
    conn.execute("update message set is_automated = true")
    conn.commit()
    t0 = time.monotonic()
    p = unlock.compute_fruit(conn)
    took = time.monotonic() - t0
    assert p["undecided"] == 20_000 and p["groups"] == 2_101 and took < 8, took   # 2,100 filler senders and oskar@
    c = client(database, vault)
    first = c.get("/api/unlock/fruit").json()
    assert len(first["top"]) == unlock.TOP
    calls = []
    monkeypatch.setattr(unlock, "groups", lambda *a, **k: calls.append("sync") or {})
    monkeypatch.setattr(insight, "refresh_behind", lambda *a, **k: calls.append("behind") or True)
    monkeypatch.setattr(unlock, "fruit_fingerprint", lambda c: insight.read(c, unlock.FRUIT)["fingerprint"])
    t0 = time.monotonic()
    again = c.get("/api/unlock/fruit").json()
    assert time.monotonic() - t0 < 0.5 and again["top"] == first["top"] and calls == []
    monkeypatch.setattr(unlock, "fruit_fingerprint", lambda c: "moved")
    stale = c.get("/api/unlock/fruit").json()
    assert stale["stale"] and stale["top"] == first["top"] and calls == ["behind"]   # shown, and computed behind it


# ---------------------------------------------------------------- improvement jobs

def test_improvement_jobs_rank_focused_runs_by_messages_with_near_misses_confusions_cost_and_commands(
        conn, ingestor, keychain, taxonomy_loaded):
    ids, _ = _accepting(conn, ingestor)
    _gold_run(conn, ids)
    p = unlock.compute_jobs(conn)
    jobs = {j["field"]: j for j in p["jobs"] if not j.get("combined")}
    assert set(jobs) == set(unlock.JOB_FIELDS) and not [j for j in p["jobs"] if "error" in j]
    assert [j["messages"] for j in p["jobs"]] == sorted((j["messages"] for j in p["jobs"]), reverse=True)
    for f, j in jobs.items():
        sel = unlock._uncertain(conn, f)
        assert j["cases"] == len(sel["cases"]) and j["messages"] == len({m for c in sel["cases"] for m in c.members})
        cmds = [s.get("command") for s in j["steps"]]
        asked = "value" if f == "keep" else f
        dry = cmds.index(f"uv run talos enrich jev focus --field {asked} --where uncertain --dry-run")
        assert cmds.index(f"uv run talos enrich jev focus --field {asked} --where uncertain") == dry + 1
        assert (f"uv run talos enrich jev focus --field {asked} --gold" in cmds) is not j["gold_checked"]
        assert j["tokens_per_case"] > 0 or not j["cases"]
        assert j["cost_usd"] == round(jev.cost(j["tokens_per_case"] * j["cases"]), 2)
    # value: Pia's context at 0.60 is just under the level (0.70): near, and undecided
    near = conn.execute("select count(distinct entity_id)::int as n from assignment where dimension_id = 'value'"
                        " and source_kind = 'model' and status = 'proposed' and confidence >= 0.6 and confidence < 0.7"
                        ).fetchone()["n"]
    assert near > 0 and jobs["value"]["near"] == near and jobs["value"]["near_band"] == [0.6, 0.7]
    # the answer key's confusions: Jev said notification where the owner said person
    conf = jobs["origin"]["confusions"]
    assert conf["items"] == 3 and conf["right"] == 2
    assert {k: conf["pairs"][0][k] for k in ("said", "owner", "items")} == {"said": "notification", "owner": "person",
                                                                          "items": 1}
    assert conf["groups"][0]["values"] == ["notification", "person"]
    assert jobs["origin"]["steps"][0]["command"] == "uv run talos taxonomy load"


def test_the_jobs_come_as_one_combined_run_too_each_case_once_with_what_it_is_unsure_of(
        conn, ingestor, keychain, taxonomy_loaded):
    _accepting(conn, ingestor)
    p = unlock.compute_jobs(conn)
    (both,) = [j for j in p["jobs"] if j.get("combined")]
    single = [j for j in p["jobs"] if not j.get("combined") and j["cases"]]
    cases = {(c.stage, c.key, c.sample): c for j in single for c in unlock._uncertain(conn, j["field"])["cases"]}
    assert both["key"] == "focus:combined:uncertain" and both["fields"] == [f for f in unlock.JOB_FIELDS
                                                                            if f in {j["field"] for j in single}]
    assert both["cases"] == len(cases) and both["separate_cases"] == sum(j["cases"] for j in single)
    assert both["messages"] == len({m for c in cases.values() for m in c.members})
    assert both["cases"] < both["separate_cases"] and 0 < both["tokens_per_case"] < both["separate_tokens_per_case"]
    spec = ",".join(both["fields"])
    cmds = [s["command"] for s in both["steps"]]
    assert f"uv run talos enrich jev focus --field {spec} --gold" in cmds          # nothing is checked yet
    assert f"uv run talos enrich jev focus --field {spec} --where uncertain --max-cases {both['cases']}" in cmds
    assert focus.run_many(conn, both["fields"], "uncertain", dry_run=True)["to_send"] == both["cases"]
    unlock.mark_job(conn, both["key"], "done")
    assert conn.execute("select field from improvement_job where key = %s", (both["key"],)).fetchone()["field"] is None


def test_a_job_marked_done_or_dismissed_disappears_and_can_be_shown_again(
        conn, ingestor, keychain, taxonomy_loaded, vault, database):
    _accepting(conn, ingestor)
    unlock.jobs(conn)                  # the web computes the jobs behind the page; here, now
    c = client(database, vault)
    first = c.get("/api/unlock/jobs").json()
    key = next(j["key"] for j in first["jobs"] if not j.get("combined"))
    assert first["hidden"] == 0 and key.startswith("focus:")
    assert c.post("/api/unlock/jobs/state", json={"key": key, "state": "done"}).status_code == 403
    assert c.post("/api/unlock/jobs/state", headers=POST, json={"key": key, "state": "done"}).json()["state"] == "done"
    now = c.get("/api/unlock/jobs").json()
    assert key not in [j["key"] for j in now["jobs"]] and now["hidden"] == 1
    assert conn.execute("select state, field from improvement_job where key = %s", (key,)).fetchone() == {
        "state": "done", "field": key.split(":")[1]}
    shown = c.get("/api/unlock/jobs?hidden=1").json()
    assert next(j for j in shown["jobs"] if j["key"] == key)["state"] == "done"
    assert c.post("/api/unlock/jobs/state", headers=POST, json={"key": key, "state": "gone"}).status_code == 400
    c.post("/api/unlock/jobs/state", headers=POST, json={"key": key, "state": None})
    assert key in [j["key"] for j in c.get("/api/unlock/jobs").json()["jobs"]]
    unlock.mark_job(conn, key, "dismissed")
    conn.commit()
    assert key not in [j["key"] for j in unlock.jobs(conn)["jobs"]]


def test_no_backfill_yet_gives_no_jobs_and_says_what_comes_first(conn):
    p = unlock.compute_jobs(conn)
    assert p["jobs"] == [] and "backfill" in p["note"]


def test_the_first_jobs_load_never_waits_for_the_half_minute_computation(conn, vault, database, monkeypatch):
    calls = []
    monkeypatch.setattr(insight, "refresh_behind", lambda dsn, name, *a: calls.append(name) or True)
    d = client(database, vault).get("/api/unlock/jobs").json()
    assert d["jobs"] == [] and d["refreshing"] and "behind the page" in d["note"] and calls == [unlock.JOBS]


def test_answers_from_claude_are_applied_as_model_values_never_as_his(conn):
    from talos import gold
    assert gold.source_for(personal.OWNER_ID) == ("human", "gold")
    assert gold.source_for("claude") == ("model", "claude-gold")
    assert gold.source_for("haiku") == ("model", "haiku-gold")
