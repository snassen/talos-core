"""Rules v2: OR groups and 'in' lists, removing a rule (with its history, and its suggestion coming
back), the suggestions' drill-down with counts per level, a group rule from a ticked value or category,
and the rule file of 2026-09-27 loaded as the lead will load it."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mailfactory as mf
import pytest
from starlette.testclient import TestClient

from talos import cli, objects, personal, rules, suggest
from talos.config import Settings
from talos.ingest import Location
from talos.web import app

H = {"X-Talos": "1"}
T0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
N = {"uid": 0}
RULE_FILE = Path(__file__).parent.parent / "rules" / "example-rules.json"  # the shape of the owner's rules, invented senders


def mails(conn, ingestor, frm, subjects, **values):
    """One message per subject from frm, each with these values accepted from Jev (model, active)."""
    ids = []
    for s in subjects:
        N["uid"] += 1
        at = T0 + timedelta(hours=N["uid"])
        mid = ingestor.ingest("gmail", mf.make(frm=frm, subject=s, date=at),
                              Location("[all]", f"v{N['uid']}", uidvalidity=1, uid=N["uid"], received_at=at)).message_id
        for dim, value in values.items():
            conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, status)"
                         " values (%s, %s, %s, 'model', 'run-1', 'active')", (mid, dim, value))
        ids.append(mid)
    conn.commit()
    return ids


def ids_of(conn, conditions):
    where, params = rules.compile_conditions(conditions)
    return {r["id"] for r in conn.execute(f"select m.id from message m where {where}", params)}


@pytest.fixture
def taxonomy(conn, taxonomy_loaded):
    yield
    conn.rollback()
    conn.execute("delete from assignment")
    conn.commit()


@pytest.fixture
def archive(conn, ingestor, taxonomy):
    """Three alerting senders (one of them by subject pattern), a backup topic sender, and noise."""
    a = mails(conn, ingestor, "Monitor <alert@monitor.example>", [f"Disk {i} full" for i in range(20)],
              kind="alert", topic="Work/Monitoring")
    b = mails(conn, ingestor, "Uptime <down@uptime.example>", [f"Site {i} is down" for i in range(22)], kind="alert")
    # A sender that conflicts as a whole; its "[failed] backup job" pattern agrees on alert.
    c = mails(conn, ingestor, "NAS <nas@nas.example>", [f"[Failed] Backup job {i}" for i in range(21)],
              kind="alert", topic="Work/Backup")
    mails(conn, ingestor, "NAS <nas@nas.example>", ["Weekly summary", "Weekly summary again"], kind="report")
    d = mails(conn, ingestor, "Store <shop@store.example>", [f"Order {i}" for i in range(20)], kind="transaction")
    conn.commit()
    return {"monitor": a, "uptime": b, "nas": c, "shop": d}


def client(vault, database):
    return TestClient(app.create(Settings(home=vault.root.parent, dsn=database), allowed_hosts=["testserver"]))


# ------------------------------------------------------------------ conditions: 'in' and OR groups

def test_an_in_list_and_an_any_group_compile_and_match_the_union_of_their_alternatives(conn, archive):
    monitor, uptime, nas = set(archive["monitor"]), set(archive["uptime"]), set(archive["nas"])
    assert ids_of(conn, [{"field": "from_address", "op": "in", "value": ["ALERT@monitor.example", "down@uptime.example"]}]) \
        == monitor | uptime
    group = [{"any": [{"field": "from_address", "op": "in", "value": ["alert@monitor.example"]},
                      [{"field": "from_address", "op": "is", "value": "nas@nas.example"},
                       {"field": "subject", "op": "matches", "value": r"^\[failed\]"}]]}]
    assert ids_of(conn, group) == monitor | nas
    # nested: an any inside an alternative, and an any ANDed with a plain condition
    nested = [{"field": "subject", "op": "contains", "value": "1"},
              {"any": [{"field": "from_domain", "op": "is", "value": "uptime.example"},
                       [{"any": [{"field": "from_address", "op": "is", "value": "alert@monitor.example"}]}]]}]
    got = ids_of(conn, nested)
    assert got and got <= monitor | uptime
    assert rules.preview(conn, group)["count"] == len(monitor | nas)


def test_a_malformed_group_is_refused_with_a_rule_error(conn):
    for bad in ([{"any": []}], [{"any": "x"}], [{"any": [[]]}], ["x"],
                [{"field": "from_address", "op": "in", "value": "not a list"}]):
        with pytest.raises(rules.RuleError):
            rules.compile_conditions(bad)
    with pytest.raises(rules.RuleError):  # a broken regex deep inside a group is caught by check()
        rules.check(conn, [{"any": [[{"field": "subject", "op": "matches", "value": "(("}]]}])


# ------------------------------------------------------------------ removing a rule

def test_removing_a_rule_deletes_its_values_and_memberships_and_the_message_falls_back(conn, archive):
    m = archive["monitor"][0]
    obj = objects.create(conn, "area", "Alerts")
    cond = [{"field": "from_address", "op": "is", "value": "alert@monitor.example"}]
    rules.save(conn, rules.Rule(id="mon.kind", name="Monitor is a report", conditions=cond,
                                action={"dimension": "kind", "value": "report"}))
    rules.save(conn, rules.Rule(id="mon.obj", name="Monitor in Alerts", conditions=cond, action={"object": obj}))
    conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, status, decided_by)"
                 " values (%s, 'topic', 'Work/Network', 'human', %s, 'active', %s)", (m, personal.OWNER_ID, personal.OWNER_ID))
    rules.run_all(conn)
    conn.commit()
    eff = lambda: conn.execute("select value, source_kind from effective_message_assignment"
                               " where message_id = %s and dimension_id = 'kind'", (m,)).fetchone()
    assert eff()["value"] == "report" and eff()["source_kind"] == "rule"
    # an older version's values go too
    rules.save(conn, rules.Rule(id="mon.kind", name="Monitor is a report", conditions=cond + [
        {"field": "subject", "op": "contains", "value": "disk"}], action={"dimension": "kind", "value": "report"}))
    conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, status)"
                 " values (%s, 'kind', 'report', 'rule', 'rule:mon.kind@1', 'superseded')", (m,))

    res = rules.remove(conn, "mon.kind", why="not a report")
    conn.commit()
    assert res["values"] == 21 and res["version"] == 2
    assert eff()["value"] == "alert" and eff()["source_kind"] == "model"         # back to the accepted Jev value
    assert conn.execute("select count(*) n from assignment where source_ref like 'rule:mon.kind@%%'").fetchone()["n"] == 0
    assert conn.execute("select count(*) n from assignment where source_kind = 'human'").fetchone()["n"] == 1
    assert rules.remove(conn, "mon.obj")["edges"] == 20
    assert conn.execute("select count(*) n from edge where source like 'rule:mon.obj@%%'").fetchone()["n"] == 0
    assert conn.execute("select count(*) n from rule").fetchone()["n"] == 0
    with pytest.raises(rules.RuleError):
        rules.remove(conn, "mon.kind")


def test_a_removed_rule_is_kept_in_the_history_with_its_definition_when_and_why(conn, archive):
    rules.save(conn, rules.Rule(id="a.one", name="One", priority=7, description="because",
                                conditions=[{"field": "from_domain", "op": "is", "value": "store.example"}],
                                action={"dimension": "kind", "value": "transaction"}))
    rules.remove(conn, "a.one", why="not interesting")
    conn.commit()
    [h] = rules.removed(conn)
    assert h["rule_id"] == "a.one" and h["why"] == "not interesting" and h["removed_at"] is not None
    d = h["definition"]
    assert d["name"] == "One" and d["priority"] == 7 and d["description"] == "because" and d["version"] == 1
    assert d["conditions"] == [{"field": "from_domain", "op": "is", "value": "store.example"}]
    assert d["action"] == {"dimension": "kind", "value": "transaction"}
    # it can be put back from its definition
    rules.save(conn, rules.Rule(**{k: d[k] for k in rules.RULE_FIELDS}))
    assert rules.load(conn)[0].id == "a.one"


def test_remove_all_keeps_the_named_rules_and_refuses_an_unknown_keep(conn, archive):
    for i in range(3):
        rules.save(conn, rules.Rule(id=f"r{i}", name=f"R{i}", conditions=[{"field": "subject", "op": "contains", "value": str(i)}],
                                    action={"dimension": "kind", "value": "alert"}))
    with pytest.raises(rules.RuleError):
        rules.remove_all(conn, keep=["r1", "typo"])
    assert conn.execute("select count(*) n from rule").fetchone()["n"] == 3        # nothing removed
    done = rules.remove_all(conn, keep=["r1"], why="a fresh start")
    conn.commit()
    assert [d["id"] for d in done] == ["r0", "r2"]
    assert [r.id for r in rules.load(conn, enabled_only=False)] == ["r1"]
    assert {h["rule_id"] for h in rules.removed(conn)} == {"r0", "r2"}


def test_a_removed_suggestion_rule_comes_back_among_the_suggestions(conn, archive, vault, database):
    c = client(vault, database)
    page = c.get("/api/rules/suggestions", params={"category": "kind:alert"}).json()
    row = next(r for r in page["rows"] if r["sender"] == "down@uptime.example")
    assert c.post("/api/rules/suggestions/add", json={"id": row["id"]}, headers=H).status_code == 201
    ids = lambda: {r["id"] for r in c.get("/api/rules/suggestions", params={"category": "kind:alert"}).json()["rows"]}
    assert row["id"] not in ids()
    # switched on and run: its values now come from the rule, and the suggestion stays away
    assert c.post(f"/api/rules/{row['id']}/enabled", json={"enabled": True}, headers=H).status_code == 200
    assert c.post("/api/rules/run", headers=H).status_code == 200
    assert row["id"] not in ids()
    assert c.post(f"/api/rules/{row['id']}/remove", json={"why": "changed my mind"}).status_code == 403
    r = c.post(f"/api/rules/{row['id']}/remove", json={"why": "changed my mind"}, headers=H)
    assert r.status_code == 200 and r.json()["values"] == 22
    assert row["id"] in ids()                                                  # it is a suggestion again
    [h] = c.get("/api/rules/removed").json()
    assert h["rule_id"] == row["id"] and h["why"] == "changed my mind"
    assert h["definition"]["origin"]["suggestion"] == row["id"]
    assert c.post("/api/rules/nope/remove", json={}, headers=H).status_code == 404


# ------------------------------------------------------------------ the drill-down

def test_the_drill_down_counts_suggestions_and_coverage_at_every_level(conn, archive):
    rows = suggest.draft(conn)
    cover = suggest.coverage(conn, rows)
    fields = {f["dimension"]: f for f in suggest.tree(conn, rows, cover)}
    kind = fields["kind"]
    alert = next(c for c in kind["categories"] if c["key"] == "kind:alert")
    assert alert["single"] is True and [v["value"] for v in alert["values"]] == ["alert"]
    senders = {s["sender"]: s for s in alert["values"][0]["suggestions"]}
    assert set(senders) == {"alert@monitor.example", "down@uptime.example", "nas@nas.example"}
    assert senders["nas@nas.example"]["skeleton"] == "failed backup job"
    # each suggestion's coverage is what its rule would match
    for s in rows:
        assert cover[s["id"]] == rules.preview(conn, s["conditions"])["count"], s["name"]
    assert senders["alert@monitor.example"]["coverage"] == 20 and senders["nas@nas.example"]["coverage"] == 21
    # every level adds up
    for f in fields.values():
        assert f["count"] == sum(c["count"] for c in f["categories"])
        assert f["coverage"] == sum(c["coverage"] for c in f["categories"])
        for cat in f["categories"]:
            assert cat["count"] == sum(v["count"] for v in cat["values"])
            assert cat["coverage"] == sum(v["coverage"] for v in cat["values"])
            for v in cat["values"]:
                assert v["count"] == len(v["suggestions"])
                assert v["coverage"] == sum(s["coverage"] for s in v["suggestions"])
    assert alert["count"] == 3 and alert["coverage"] == 20 + 22 + 21
    # topic groups by family: Work holds two values
    work = next(c for c in fields["topic"]["categories"] if c["key"] == "topic:Work")
    assert work["single"] is False and {v["value"] for v in work["values"]} == {"Work/Monitoring", "Work/Backup"}
    # largest coverage first
    covs = [c["coverage"] for c in kind["categories"]]
    assert covs == sorted(covs, reverse=True)


def test_the_tree_route_sends_counts_and_filters_by_sender_or_value(conn, archive, vault, database):
    c = client(vault, database)
    t = c.get("/api/rules/suggestions/tree").json()
    assert t["total"] == t["shown"] and t["fields"][0]["dimension"] == "kind"
    assert all(not v["suggestions"] for f in t["fields"] for cat in f["categories"] for v in cat["values"])
    f = c.get("/api/rules/suggestions/tree", params={"q": "uptime"}).json()
    got = [(fl["dimension"], s["sender"]) for fl in f["fields"] for cat in fl["categories"] for v in cat["values"]
           for s in v["suggestions"]]
    assert got and all(s == "down@uptime.example" for _, s in got)
    by_value = c.get("/api/rules/suggestions/tree", params={"q": "work/backup"}).json()
    assert [v["value"] for fl in by_value["fields"] for cat in fl["categories"] for v in cat["values"]] == ["Work/Backup"]
    page = c.get("/api/rules/suggestions", params={"category": "topic:Work", "value": "Work/Backup"}).json()
    assert page["count"] == 1 and page["rows"][0]["sender"] == "nas@nas.example"


# ------------------------------------------------------------------ a group rule from a tick

def test_a_ticked_value_makes_one_rule_whose_coverage_is_the_union_of_its_members(conn, archive):
    rows = suggest.draft(conn)
    members = [s for s in rows if s["dimension"] == "kind" and s["value"] == "alert"]
    assert len(members) == 3
    rule = suggest.group_rule(conn, rows, "kind", "alert")
    assert rule.enabled is False and rule.priority == suggest.PRIORITY
    assert rule.name == "Kind Alert: 3 senders, 1 by subject pattern"
    [group] = rule.conditions
    assert group["any"][0] == {"field": "from_address", "op": "in", "value": ["alert@monitor.example", "down@uptime.example"]}
    assert group["any"][1][0] == {"field": "from_address", "op": "is", "value": "nas@nas.example"}
    rules.check(conn, rule.conditions)                                           # it compiles
    union = set().union(*(ids_of(conn, m["conditions"]) for m in members))
    assert ids_of(conn, rule.conditions) == union and len(union) == 63          # not the NAS's weekly summaries
    p = suggest.group_preview(conn, rule)
    assert p["count"] == 63 and p["conflicts"] == 0 and p["agree"] == 63 and p["members"] == 3 and not p["exists"]
    # a value that conflicts somewhere is counted before anything is saved
    conn.execute("update assignment set value = 'fyi' where entity_id = %s and dimension_id = 'kind'",
                 (archive["uptime"][0],))
    assert suggest.group_preview(conn, rule)["conflicts"] == 1


def test_a_saved_group_rule_covers_its_members_and_removing_it_brings_them_back(conn, archive, vault, database):
    c = client(vault, database)
    pre = c.post("/api/rules/suggestions/group/preview", json={"dimension": "kind", "value": "alert"}, headers=H).json()
    [p] = pre["rules"]
    assert p["count"] == 63 and p["conflicts"] == 0 and p["senders"] == 3
    assert conn.execute("select count(*) n from rule").fetchone()["n"] == 0      # a preview saves nothing
    assert c.post("/api/rules/suggestions/group", json={"dimension": "kind", "value": "alert"}).status_code == 403
    made = c.post("/api/rules/suggestions/group", json={"dimension": "kind", "value": "alert"}, headers=H)
    assert made.status_code == 201
    [r] = made.json()["rules"]
    saved = conn.execute("select * from rule where id = %s", (r["id"],)).fetchone()
    assert saved["enabled"] is False and saved["name"].startswith("Kind Alert: 3 senders")
    assert "matched 63 messages, 0 of them with another accepted value" in saved["description"]
    assert len(saved["origin"]["group"]["members"]) == 3
    tree = c.get("/api/rules/suggestions/tree").json()
    kind = next(f for f in tree["fields"] if f["dimension"] == "kind")
    alert = next(cat for cat in kind["categories"] if cat["key"] == "kind:alert")
    assert alert["count"] == 0 and alert["values"][0]["rules"][0]["id"] == r["id"]   # shown where it was made
    assert alert["values"][0]["rules"][0]["kind"] == "group"
    assert c.get("/api/rules/suggestions", params={"category": "kind:alert"}).json()["count"] == 0
    assert c.post(f"/api/rules/{r['id']}/remove", json={}, headers=H).status_code == 200
    assert c.get("/api/rules/suggestions", params={"category": "kind:alert"}).json()["count"] == 3


def test_a_ticked_category_of_several_values_makes_one_rule_per_value(conn, archive, vault, database):
    c = client(vault, database)
    pre = c.post("/api/rules/suggestions/group/preview", json={"category": "topic:Work"}, headers=H).json()
    assert {r["value"] for r in pre["rules"]} == {"Work/Monitoring", "Work/Backup"}
    assert pre["count"] == 20 + 23          # the NAS is suggested whole for topic: its summaries have no topic
    made = c.post("/api/rules/suggestions/group", json={"category": "topic:Work"}, headers=H).json()["rules"]
    assert {m["id"] for m in made} == {suggest.group_id("topic", "Work/Monitoring"), suggest.group_id("topic", "Work/Backup")}
    assert c.post("/api/rules/suggestions/group", json={"category": "topic:Work"}, headers=H).status_code == 400  # none left
    assert c.post("/api/rules/suggestions/group", json={}, headers=H).status_code == 400


# ------------------------------------------------------------------ the rule file of 2026-09-27

def test_the_new_rule_file_loads_every_rule_compiles_and_every_value_is_allowed(conn, taxonomy):
    data = json.loads(RULE_FILE.read_text())
    assert len(data["rules"]) == 20
    dims = {d["id"]: d for d in conn.execute("select id, allowed, cardinality from dimension")}
    for r in data["rules"]:
        rules.check(conn, r["conditions"])
        a = r["action"]
        assert a["dimension"] in dims, r["id"]
        allowed = dims[a["dimension"]]["allowed"]
        assert allowed is None or a["value"] in allowed, r["id"]
        assert len(r["description"]) > 200 and "Why:" in r["description"], r["id"]
        assert isinstance(r["enabled"], bool) and isinstance(r["priority"], int)
    prefixes = [r["id"].split(".")[0] for r in data["rules"]]
    assert prefixes.count("backup") == 3 and prefixes.count("unifi-gmail") == 2
    done = rules.load_file(conn, RULE_FILE)
    conn.commit()
    assert [d["state"] for d in done] == ["created"] * 20
    assert sum(r.enabled for r in rules.load(conn, enabled_only=False)) == sum(r["enabled"] for r in data["rules"])
    assert {d["state"] for d in rules.load_file(conn, RULE_FILE)} == {"unchanged"}   # idempotent
    rules.run_all(conn)                                                                 # and they run


def test_loading_a_changed_file_bumps_only_the_changed_rule(conn, taxonomy, tmp_path):
    f = tmp_path / "r.json"
    base = [{"id": "x.a", "name": "A", "conditions": [{"field": "subject", "op": "contains", "value": "a"}],
             "action": {"dimension": "kind", "value": "alert"}, "priority": 10, "enabled": True, "description": "d"},
            {"id": "x.b", "name": "B", "conditions": [{"field": "subject", "op": "contains", "value": "b"}],
             "action": {"dimension": "kind", "value": "report"}, "priority": 10, "enabled": False, "description": "d"}]
    f.write_text(json.dumps({"rules": base}))
    rules.load_file(conn, f)
    base[0]["conditions"][0]["value"] = "aa"
    base[1]["name"] = "B renamed"
    f.write_text(json.dumps({"rules": base}))
    got = {d["id"]: d for d in rules.load_file(conn, f)}
    assert got["x.a"]["state"] == "new version" and got["x.a"]["version"] == 2
    assert got["x.b"]["state"] == "updated" and got["x.b"]["version"] == 1
    base.append({**base[0], "id": "x.a"})
    f.write_text(json.dumps(base))
    with pytest.raises(rules.RuleError):
        rules.load_file(conn, f)                                   # an id twice
    f.write_text(json.dumps([{**base[0], "id": "x.c", "action": {"dimension": "kind", "value": "nonsense"}}]))
    with pytest.raises(rules.RuleError):
        rules.load_file(conn, f)
    assert conn.execute("select count(*) n from rule where id = 'x.c'").fetchone()["n"] == 0   # all or none


def test_the_command_line_loads_removes_and_lists_the_removed(conn, taxonomy, database, vault, monkeypatch, capsys):
    monkeypatch.setenv("TALOS_DSN", database)
    monkeypatch.setenv("TALOS_HOME", str(vault.root.parent))
    monkeypatch.setattr(cli, "_logging", lambda settings, verbose: None)
    rules.save(conn, rules.Rule(id="old.seed", name="Old", conditions=[{"field": "subject", "op": "contains", "value": "x"}],
                                action={"dimension": "kind", "value": "alert"}))
    conn.commit()
    cli.main(["rules", "remove", "--all", "--why", "not interesting"])
    assert "1 rule removed" in capsys.readouterr().out
    cli.main(["rules", "load", str(RULE_FILE)])
    assert "20 created" in capsys.readouterr().out
    cli.main(["rules", "load", str(RULE_FILE)])
    assert "20 unchanged" in capsys.readouterr().out
    cli.main(["rules", "remove", "school.topic", "--why", "later"])
    cli.main(["rules", "removed"])
    out = capsys.readouterr().out
    assert "school.topic@1" in out and "old.seed@1" in out and "(not interesting)" in out
    cli.main(["rules", "remove", "--all", "--keep", "backup.topic,uptime.down-alert"])
    conn.rollback()
    assert {r["id"] for r in conn.execute("select id from rule")} == {"backup.topic", "uptime.down-alert"}
    with pytest.raises(SystemExit):
        cli.main(["rules", "remove", "no.such.rule"])
