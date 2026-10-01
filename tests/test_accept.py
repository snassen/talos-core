"""Two-level acceptance (talos.boundary, backfill.accept), the acceptance explorer
(talos.acceptance, /api/acceptance) and focused Jev runs (talos.focus).

Never the real Jev: every request goes to test_jev's fake TypeSafe endpoint (httpx.MockTransport).
Invented mail only.
"""

import json

import pytest
from test_backfill import _archive, _pick, _rows, client
from test_enrich import mail
from test_jev import FakeJev, keychain  # noqa: F401  (keychain is a fixture)
from test_web import client as web

from talos import acceptance, backfill, boundary, cli, db, facets, focus, gold, jev, rules, search, taxonomy

pytestmark = pytest.mark.usefixtures("taxonomy_loaded")

TWO = backfill.TWO_LEVEL


def _run(conn, stage, pick=_pick, **kw):
    fake = FakeJev(pick=pick)
    kw.setdefault("progress", lambda s: None)
    return backfill.run(conn, stage, client=client(fake), **kw), fake


def _eff(conn, mid, dim):
    r = conn.execute("select value, source_kind from effective_message_assignment where message_id = %s"
                     " and dimension_id = %s", (mid, dim)).fetchone()
    return (r["value"], r["source_kind"]) if r else None


def _gate(conn, field):
    """Pretend the focused question was asked of the answer key (the gate an archive run needs)."""
    qv = focus.question_version(focus.question_sets(focus.dims_for(conn, field), field))
    conn.execute("insert into model_run (id, model, purpose, params) values (%s, 'x', 'gold-eval', %s)",
                 (f"gold-gate-{field}", json.dumps({"set_id": 1, "focus": {"field": field}, "question_version": qv})))
    conn.commit()


# ---------------------------------------------------------------- boundaries

def test_a_boundary_is_the_sum_of_jevs_full_probabilities_not_of_the_top_three(conn, ingestor, keychain):
    ids = _archive(conn, ingestor)
    sd = boundary.sides(conn)
    assert sd["sender_kind"]["side_of"]["person_via_system"] == "people" and sd["sender_kind"]["side_of"]["list"] == "people"
    assert sd["sphere"]["side_of"]["Work/Backup"] == "work" and sd["sphere"]["side_of"]["Finance/Payments"] == "personal"
    assert sd["form"]["side_of"]["fyi"] == "conversation" and sd["form"]["side_of"]["invoice"] == "other"
    assert sd["keep"]["side_of"]["record_financial"] == "keep" and sd["keep"]["side_of"]["noise"] == "short_lived"
    probs = boundary.side_probabilities({"person": 0.4, "list": 0.2, "notification": 0.3, "spam": 0.1},
                                        sd["sender_kind"]["side_of"], sd["sender_kind"]["sides"])
    assert probs == {"people": 0.6, "machine": 0.4} and boundary.decide(probs)[:3] == ("people", 0.6, 0.2)

    def pick(body):  # the invoices: notification at 0.5, the other ten origins 0.05 each
        return {**_pick(body), "origin": ("notification", 0.5)} if "Faktura nr" in body["state"].get("text", "") \
            else _pick(body)
    res, _ = _run(conn, "machine", pick)
    sk = _rows(conn, res["run_id"], "sender_kind")
    row = sk[(ids["invoices"][1], "sender_kind", "machine")]
    # machine: 0.5 + seven machine values × 0.05 = 0.85; the top three would have said 0.6
    assert row["confidence"] == pytest.approx(0.85, abs=1e-4) and row["status"] == "proposed"
    ev = row["evidence"]
    assert ev["scores"] == {"machine": 0.85, "people": 0.15} and ev["boundary_of"] == "origin"
    assert ev["margin"] == pytest.approx(0.7) and ev["decided"] is False and ev["propagated"] is True
    origin_top3 = _rows(conn, res["run_id"], "origin")[(ids["invoices"][1], "origin", "notification")]["evidence"]["scores"]
    assert len(origin_top3) == 3 and sum(origin_top3.values()) == pytest.approx(0.6)
    # the value split between the samples (record_financial and transient): keep splits too, so only
    # the samples carry it; sphere (Finance/Payments, personal) agrees and reaches all five
    keep = {(e, v) for (e, f, v) in _rows(conn, res["run_id"], "keep")} & {(i, v) for i in ids["invoices"]
                                                                          for v in ("keep", "short_lived")}
    inv = ids["invoices"]
    assert keep == {(inv[0], "short_lived"), (inv[2], "keep"), (inv[4], "short_lived")}
    assert {e for (e, f, v) in _rows(conn, res["run_id"], "sphere") if v == "personal"} >= set(inv)


def test_the_taxonomy_maps_every_family_of_a_boundarys_source_or_refuses_the_file(conn, tmp_path):
    data = json.loads(taxonomy.DEFAULT_PATH.read_text(encoding="utf-8"))
    del data["dimensions"]["sphere"]["derived_from"]["families"]["Leisure"]
    (tmp_path / "t.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(taxonomy.TaxonomyError, match="families of topic map to nothing: Leisure"):
        taxonomy.load(conn, tmp_path / "t.json")
    data["dimensions"]["sphere"]["derived_from"]["families"]["Leisure"] = "somewhere"
    (tmp_path / "t.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(taxonomy.TaxonomyError, match="unknown value: Leisure"):
        taxonomy.load(conn, tmp_path / "t.json")
    d = conn.execute("select derived_from, allowed from dimension where id = 'keep'").fetchone()
    assert d["derived_from"]["dimension"] == "value" and d["allowed"] == ["keep", "short_lived"]


# ---------------------------------------------------------------- two-level accept

def test_two_level_accept_takes_boundaries_at_090_exact_values_at_070_teams_as_people_and_unaccept_undoes_it(
        conn, ingestor, keychain):
    ids = _archive(conn, ingestor)
    runs = [_run(conn, st)[0]["run_id"] for st in backfill.STAGES]
    pia = ids["pia"][-1]
    dry = backfill.accept(conn, runs, TWO, dry_run=True)
    assert dry["fields"]["sender_kind"]["promoted"] > 0
    assert {r["status"] for rid in runs for r in _rows(conn, rid).values()} == {"proposed"}  # rolled back
    out = backfill.accept(conn, runs, TWO)
    assert out["decided_by"] == ("policy:origin>=0.70,type>=0.70,topic>=0.70,value>=0.70,sender_kind>=0.90,"
                                 "sphere>=0.90,form>=0.90,keep>=0.90,kind>=0.85,margin>=0.15")
    assert _eff(conn, pia, "type") == ("request", "model")              # 0.80: the old preset (0.90) would not
    assert _eff(conn, pia, "value") is None                             # context at 0.60 stays a proposal
    assert _eff(conn, pia, "sender_kind") == ("people", "model")        # person 0.97 + two people values
    assert _eff(conn, pia, "sphere") == ("work", "model")
    # Teams: no origin is asked; sender_kind is people, fixed, for every line (the owner's own too)
    teams = _rows(conn, runs[2])
    assert not any(f == "origin" for (_, f, _) in teams)
    chat = ids["chat"] + ids["long"]
    fixed = {e: r for (e, f, v), r in teams.items() if f == "sender_kind"}
    assert set(fixed) == set(chat) and {r["value"] for r in fixed.values()} == {"people"}
    assert all(r["status"] == "active" and r["confidence"] == 1.0 and r["evidence"]["fixed"] for r in fixed.values())
    assert conn.execute(f"select ({search.machine_sql()}) as x from message m where m.id = %s",
                        (chat[1],)).fetchone()["x"] is False
    stamp = conn.execute("select params->'accept' as a from model_run where id = %s", (runs[0],)).fetchone()["a"]
    assert stamp["thresholds"] == TWO and stamp["decided_by"] == out["decided_by"]
    undone = backfill.unaccept(conn, runs)
    assert undone["fields"]["sender_kind"] == out["fields"]["sender_kind"]["active"]
    assert {r["status"] for rid in runs for r in _rows(conn, rid).values()} == {"proposed"}
    assert _eff(conn, pia, "type") is None and _eff(conn, pia, "sender_kind") is None
    assert conn.execute("select count(*) as n from model_run where params ? 'accept'").fetchone()["n"] == 0


def test_an_exact_value_is_accepted_only_on_the_side_of_its_accepted_boundary(conn, ingestor, keychain):
    ids = _archive(conn, ingestor)

    def pick(body):  # Pia is a person at 0.75: people add up to 0.80, under the boundary's 0.90
        return {**_pick(body), "origin": ("person", 0.75)} if "Pia skriver" in body["state"].get("text", "") \
            else _pick(body)
    res, _ = _run(conn, "person", pick)
    pia = ids["pia"]
    assert _rows(conn, res["run_id"], "sender_kind")[(pia[0], "sender_kind", "people")]["confidence"] == \
        pytest.approx(0.80, abs=1e-4)
    # the boundary undecided: the exact value is accepted on its own
    backfill.accept(conn, res["run_id"], TWO)
    assert _eff(conn, pia[0], "origin") == ("person", "rule")    # the pre-pass still wins where it has spoken
    assert _rows(conn, res["run_id"], "origin")[(pia[0], "origin", "person")]["status"] == "active"
    # a decided boundary on the other side (the owner's: machine) holds the exact value back, and only there
    rules.assign(conn, [pia[0]], "sender_kind", "machine")
    out = backfill.accept(conn, res["run_id"], TWO)
    o = _rows(conn, res["run_id"], "origin")
    assert o[(pia[0], "origin", "person")]["status"] == "proposed" and out["fields"]["origin"]["demoted"] == 1
    assert o[(pia[1], "origin", "person")]["status"] == "active"
    assert out["fields"]["origin"]["held_back"] == 1
    assert "1 held back: their side disagrees with the boundary" in backfill.format_accept(out)
    # the same value, the same side: nothing held back
    rules.assign(conn, [pia[0]], "sender_kind", "people")
    assert backfill.accept(conn, res["run_id"], TWO)["fields"]["origin"]["held_back"] == 0
    assert _rows(conn, res["run_id"], "origin")[(pia[0], "origin", "person")]["status"] == "active"


# ---------------------------------------------------------------- Messages

def _sk(conn, mid, value, kind="model", dim="sender_kind", ref="jev-x"):
    if kind == "human":
        rules.assign(conn, [mid], dim, value)
        return
    conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref)"
                 " values (%s, %s, %s, %s, %s)", (mid, dim, value, kind, ref))


def test_people_and_automated_go_by_sender_kind_then_origin_then_the_flag_human_over_rule_over_model(conn, ingestor):
    def m(name, **kw):
        return mail(ingestor, frm=f"{name} <{name.lower()}@example.org>", subject=name, **kw)
    auto = {"Auto-Submitted": "auto-generated"}
    x = {k: m(k) for k in ("A", "B", "C", "E", "F")}
    x["D"] = m("D", headers=auto)
    x["G"] = m("G", thread="g")
    _sk(conn, x["A"], "machine")                                    # model sender_kind beats model origin
    _sk(conn, x["A"], "person", dim="origin")
    _sk(conn, x["B"], "machine")                                    # a rule's origin beats a model's sender_kind
    _sk(conn, x["B"], "person", kind="rule", dim="origin", ref="prepass:origin.test@1")
    _sk(conn, x["C"], "people", kind="human")                       # the owner's sender_kind beats a rule's origin
    _sk(conn, x["C"], "notification", kind="rule", dim="origin", ref="prepass:origin.test@1")
    _sk(conn, x["E"], "marketing", dim="origin")                    # origin alone
    _sk(conn, x["F"], "machine")                                    # sender_kind alone
    tid = conn.execute("select thread_id from message where id = %s", (x["G"],)).fetchone()["thread_id"]
    _sk(conn, x["G"], "machine")                                    # the owner's value on the thread beats the message's
    _sk(conn, tid, "people", kind="human")
    conn.commit()
    want = {"A": True, "B": False, "C": False, "D": True, "E": True, "F": True, "G": False}
    one = {k: conn.execute(f"select ({search.machine_sql()}) as x from message m where m.id = %s",
                           (i,)).fetchone()["x"] for k, i in x.items()}
    assert one == want
    ids = {i: k for k, i in x.items()}
    for flag in (True, False):  # the selection form (sender_state) agrees with the one-message form
        got = {ids[r["id"]] for r in search.messages(conn, automated=flag, limit=100)["rows"] if r["id"] in ids}
        assert got == {k for k, v in want.items() if v is flag}
    senders = {r["address"]: r["automated"] for r in facets.senders(conn, limit=0)["rows"]}
    assert {k: senders[f"{k.lower()}@example.org"] for k in want} == want


def test_the_work_personal_and_keep_filters_and_facets_follow_the_effective_value(conn, ingestor, vault, database):
    a, b, c = (mail(ingestor, frm=f"N{i} <n{i}@example.org>", subject=f"S{i}") for i in range(3))
    d = mail(ingestor, frm="T <t@example.org>", subject="T", thread="t")
    _sk(conn, a, "work", dim="sphere")
    _sk(conn, b, "work", dim="sphere")
    _sk(conn, b, "personal", kind="human", dim="sphere")      # the owner's value wins over the model's
    _sk(conn, c, "personal", dim="sphere")
    tid = conn.execute("select thread_id from message where id = %s", (d,)).fetchone()["thread_id"]
    _sk(conn, tid, "work", kind="human", dim="sphere")        # set on the thread
    _sk(conn, a, "keep", dim="keep")
    conn.commit()
    def ids(**f):
        return {r["id"] for r in search.messages(conn, limit=100, **f)["rows"]}
    assert ids(dimension=("sphere", "work")) == {a, d}
    assert ids(dimension=("sphere", "personal")) == {b, c}
    assert ids(dimension=[("sphere", "work"), ("keep", "keep")]) == {a}
    view = {r["message_id"] for r in conn.execute("select message_id from effective_message_assignment"
                                                  " where dimension_id = 'sphere' and value = 'work'")}
    assert view == {a, d}
    r = web(database, vault).get("/api/facets", params=[("dim", "keep:keep")]).json()
    assert {x["value"]: x["n"] for x in r["sphere"]} == {"work": 1}
    assert {(x["value"], x["label"]) for x in r["keep"]} == {("keep", "Worth keeping")}
    full = web(database, vault).get("/api/facets").json()
    assert {x["value"]: x["n"] for x in full["sphere"]} == {"work": 2, "personal": 2}


def test_the_message_pane_gets_its_values_with_labels_and_the_proposals_that_do_not_count_yet(
        conn, ingestor, vault, database):
    mid = mail(ingestor, frm="Oskar <oskar@nordvik.se>", subject="Offert")
    _sk(conn, mid, "people", ref="run-1")
    conn.execute("update assignment set status = 'active' where entity_id = %s and dimension_id = 'sender_kind'", (mid,))
    for dim, value, p in (("sphere", "work", 0.8), ("keep", "keep", 0.6), ("sender_kind", "machine", 0.3)):
        conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, status,"
                     " confidence) values (%s, %s, %s, 'model', 'run-2', 'proposed', %s)", (mid, dim, value, p))
    conn.commit()
    m = web(database, vault).get(f"/api/messages/{mid}").json()
    vals = {v["dimension_id"]: v for v in m["values"]}
    assert (vals["sender_kind"]["label"], vals["sender_kind"]["dimension_label"]) == ("People", "Sender")
    props = {(p["dimension_id"], p["label"]) for p in m["proposals"]}
    # sender_kind already has a value that counts: its proposal is not shown as one
    assert props == {("sphere", "Work"), ("keep", "Worth keeping")}


# ---------------------------------------------------------------- the explorer

def _accepting(conn, ingestor):
    ids = _archive(conn, ingestor)
    runs = [_run(conn, st)[0]["run_id"] for st in backfill.STAGES]
    return ids, runs


def _gold_run(conn, ids):
    """A tiny answer key over the archive with the owner's labels, and a v2 run on it (stored as a run is)."""
    conn.execute("insert into gold_set (name, seed, target) values ('k', 1, 3)")
    sid = conn.execute("select id from gold_set").fetchone()["id"]
    for pos, (mid, labels) in enumerate(((ids["pia"][-1], {"origin": "person", "type": "request"}),
                                         (ids["invoices"][2], {"origin": "person", "type": "invoice"}),
                                         (ids["letter"], {"origin": "person", "type": "question"})), 1):
        conn.execute("insert into gold_item (set_id, position, message_id, stratum, reason, unit)"
                     " values (%s, %s, %s, 'person', 'r', 'message')", (sid, pos, mid))
        for f, v in labels.items():
            gold.save_label(conn, sid, pos, f, [v])
    params = {"set_id": sid, "unit": "context", "template_version": 2, "question_version": "q"}
    conn.execute("insert into model_run (id, model, purpose, params) values ('g1', 'x', 'gold-eval', %s)",
                 (json.dumps(params),))
    items = {r["position"]: r["id"] for r in conn.execute("select id, position from gold_item where set_id = %s", (sid,))}
    rows = {1: {"origin": ("person", 0.97), "type": ("request", 0.8)},
            2: {"origin": ("notification", 0.95), "type": ("invoice", 0.93)},    # a costly error
            3: {"origin": ("person", 0.72), "type": ("conversation", 0.66)}}
    for pos, fields in rows.items():
        conn.execute("insert into jev_case (run_id, item_id, record_sha256, record_chars, model)"
                     " values ('g1', %s, 'x', 1, 'x')", (items[pos],))
        for f, (top, p) in fields.items():
            allowed = conn.execute("select allowed from dimension where id = %s", (f,)).fetchone()["allowed"]
            scores = {v: (p if v == top else (1 - p) / (len(allowed) - 1)) for v in allowed}
            conn.execute("insert into jev_prediction (run_id, item_id, field, top, selected, scores, confidence,"
                         " margin, decided, raw) values ('g1', %s, %s, %s, '{}', %s, %s, %s, true, '{}')",
                         (items[pos], f, top, json.dumps(scores), p, p - (1 - p) / (len(allowed) - 1)))
    conn.commit()
    return sid


def test_the_explorer_sends_histograms_and_answer_key_cases_that_match_the_proposals(
        conn, ingestor, vault, database, keychain):
    ids, runs = _accepting(conn, ingestor)
    _gold_run(conn, ids)
    st = web(database, vault).get("/api/acceptance").json()
    assert [f["id"] for f in st["fields"]] == ["sender_kind", "sphere", "form", "keep", "kind", "origin", "type", "topic",
                                               "value"]
    assert st["gold_run"] == "g1" and [r["id"] for r in st["runs"]] == runs
    for f in st["fields"]:
        h = st["archive"][f["id"]]
        n = conn.execute("select count(*) as n from assignment where source_kind = 'model' and dimension_id = %s"
                         " and status in ('proposed', 'active')", (f["id"],)).fetchone()["n"]
        assert sum(h["all"]) == h["total"] == n and len(h["all"]) == acceptance.BUCKETS
        # what the histogram says is over a line is what accept promotes
        t = f["default"]
        over = sum((h["all"] if f["kind"] != "exact" else h["margin_ok"])[acceptance.bucket(t):])
        res = backfill.accept(conn, runs, {f["id"]: t}, dry_run=True)
        assert res["fields"][f["id"]]["promoted"] + res["fields"][f["id"]]["held_back"] == over
    origin = st["gold"]["fields"]["origin"]
    assert [(x["i"], x["p"], x["r"], x["ps"], x["rs"]) for x in origin] == [
        (1, "person", "person", "people", "people"), (2, "notification", "person", "machine", "people"),
        (3, "person", "person", "people", "people")]
    sc = acceptance.score(origin, 0.70, margin=0.15, boundary_threshold=0.90)
    assert (sc["decided"], sc["right"], sc["costly"]) == (3, 2, 1)
    assert sc["confusions"] == {"notification→person": 1}
    assert acceptance.score(origin, 0.96, margin=0.15)["decided"] == 1
    sk = acceptance.score(st["gold"]["fields"]["sender_kind"], 0.90)
    assert (sk["n"], sk["decided"], sk["right"]) == (3, 2, 1)       # item 3: people at 0.72 + 0.056, undecided
    assert web(database, vault).get("/api/acceptance", params={"gold_run": "nope"}).status_code == 409


def test_the_explorer_examples_are_the_proposals_just_over_and_just_under_the_line(conn, ingestor, vault, database,
                                                                                   keychain):
    _accepting(conn, ingestor)
    c = web(database, vault)
    ex = c.get("/api/acceptance/examples", params={"field": "type", "t": "0.85"}).json()
    assert ex["over"] and all(r["confidence"] >= 0.85 - 1e-6 for r in ex["over"])
    assert all(r["confidence"] < 0.85 for r in ex["under"])
    assert [r["confidence"] for r in ex["over"]] == sorted(r["confidence"] for r in ex["over"])
    assert [r["confidence"] for r in ex["under"]] == sorted((r["confidence"] for r in ex["under"]), reverse=True)
    assert len(ex["over"]) <= acceptance.EXAMPLES and {"subject", "label", "from_address"} <= set(ex["over"][0])
    assert c.get("/api/acceptance/examples", params={"field": "nope", "t": "0.5"}).status_code == 400
    assert c.get("/api/acceptance/examples", params={"field": "type"}).status_code == 400


def test_apply_needs_the_header_previews_in_a_dry_run_and_accepts_the_thresholds_shown(
        conn, ingestor, vault, database, keychain):
    ids, runs = _accepting(conn, ingestor)
    c = web(database, vault)
    assert c.post("/api/acceptance/apply", json={"thresholds": TWO}).status_code == 403
    h = {"X-Talos": "1"}
    assert c.post("/api/acceptance/apply", json={"thresholds": {"type": 0.3}}, headers=h).status_code == 400
    assert c.post("/api/acceptance/apply", json={"thresholds": {"nope": 0.7}}, headers=h).status_code == 400
    pre = c.post("/api/acceptance/apply", json={"thresholds": TWO, "dry_run": True}, headers=h).json()
    assert pre["dry_run"] and pre["fields"]["type"]["promoted"] > 0
    assert conn.execute("select count(*) as n from assignment where source_kind = 'model' and status = 'active'"
                        ).fetchone()["n"] == 0
    assert c.get("/api/acceptance").json()["applied"]["thresholds"] == {}
    res = c.post("/api/acceptance/apply", json={"thresholds": {**TWO, "type": 0.95}}, headers=h).json()
    assert res["runs"] == runs and res["fields"]["type"]["threshold"] == 0.95
    assert _eff(conn, ids["pia"][-1], "type") is None                   # 0.80 < 0.95
    assert _eff(conn, ids["pia"][-1], "topic") == ("Work/IT operations", "model")
    st = c.get("/api/acceptance").json()
    assert st["applied"]["thresholds"]["type"] == 0.95 and st["applied"]["at"]
    assert st["archive"]["type"]["active"] == res["fields"]["type"]["active"]    # the cache follows the change


# ---------------------------------------------------------------- focused runs

def _sender_pick(side, p):
    def pick(body):
        qs = body["questions"]
        assert list(qs) == ["sender_kind"], "one question per case"
        return {"sender_kind": (side, p)}
    return pick


def test_a_focused_run_asks_one_question_of_the_uncertain_units_whole(conn, ingestor, keychain):
    ids = _archive(conn, ingestor)

    def pick(body):  # the invoices' origin at 0.5: their machine side is 0.85, uncertain
        return {**_pick(body), "origin": ("notification", 0.5)} if "Faktura nr" in body["state"].get("text", "") \
            else _pick(body)
    bf, _ = _run(conn, "machine", pick)
    _run(conn, "person")
    _run(conn, "teams")
    sel = focus.select(conn, "sender_kind", "uncertain")
    assert sel["stages"] == ["machine", "person"]                       # Teams is people, fixed: never asked
    got = {(c.key, c.sample) for c in sel["cases"]}
    assert {c.anchor for c in sel["cases"]} == {ids["invoices"][0], ids["invoices"][2], ids["invoices"][4]}
    assert len(got) == 3 and sel["units"] == 1                          # the template, all three samples
    stored = {(r["unit_key"], r["sample_no"]): r["record_sha256"] for r in conn.execute(
        "select unit_key, sample_no, record_sha256 from enrich_case where run_id = %s", (bf["run_id"],))}
    for c in sel["cases"]:  # the backfill's own record, rebuilt from what it stored
        assert jev.record_sha(jev.record(c.unit(conn), "context")) == stored[(c.key, c.sample)]
    # type: a threshold of its own (0.70, margin 0.15); the letter's type was asked at 0.9 by default
    t = focus.select(conn, "type", "uncertain")
    assert {c.anchor for c in t["cases"]} == set()
    assert {c.anchor for c in focus.select(conn, "value", "uncertain")["cases"]} == {ids["pia"][-1]}  # context 0.6
    _gate(conn, "sender_kind")
    fake = FakeJev(pick=_sender_pick("machine", 0.96), input_tokens=900)
    res = focus.run(conn, "sender_kind", "uncertain", client=client(fake), progress=lambda s: None)
    assert res["stored"] == 3 and len(fake.requests) == 3
    assert all(set(b["questions"]) == {"sender_kind"} for b in fake.requests)
    q = fake.requests[0]["questions"]["sender_kind"]
    assert set(q["criteria"]) == {"people", "machine"} and "platform" in q["criteria"]["people"]
    run = conn.execute("select * from model_run where id = %s", (res["run_id"],)).fetchone()
    assert run["purpose"] == "enrich-focus" and run["params"]["focus"]["field"] == "sender_kind"
    assert run["params"]["input_tokens"] == 2700
    rows = _rows(conn, res["run_id"])
    assert {f for (_, f, _) in rows} == {"sender_kind"}
    assert {e for (e, f, v) in rows if v == "machine"} == set(ids["invoices"])   # the samples agree: all five
    assert rows[(ids["invoices"][1], "sender_kind", "machine")]["confidence"] == pytest.approx(0.96)
    # asked again: nothing is uncertain any more (the chain's newest answer counts)
    assert focus.select(conn, "sender_kind", "uncertain")["cases"] == []


def test_a_focused_run_on_disagree_picks_the_cases_that_contradict_a_rule_or_pre_pass_value(conn, ingestor, keychain):
    ids = _archive(conn, ingestor)
    pia = ids["pia"][-1]
    _run(conn, "person", lambda b: {**_pick(b), "origin": ("marketing", 0.9)} if "Pia skriver" in b["state"].get("text", "")
         else _pick(b))
    assert _eff(conn, pia, "origin") == ("person", "rule")               # the pre-pass: a person
    sel = focus.select(conn, "sender_kind", "disagree")
    assert {c.anchor for c in sel["cases"]} == {pia}                    # Jev says machine, the pre-pass people
    assert {c.anchor for c in focus.select(conn, "origin", "disagree")["cases"]} == {pia}
    assert focus.select(conn, "type", "disagree")["cases"] == []
    rules.assign(conn, [ids["letter"]], "type", "invoice")              # the owner's value against Jev's conversation
    assert {c.anchor for c in focus.select(conn, "type", "disagree")["cases"]} == {ids["letter"]}
    rules.assign(conn, [pia], "sender_kind", "machine")                 # the owner's boundary agrees with Jev now
    assert focus.select(conn, "sender_kind", "disagree")["cases"] == []


def test_a_newer_run_supersedes_an_older_runs_proposals_and_its_answer_is_the_one_accepted(conn, ingestor, keychain):
    ids = _archive(conn, ingestor)

    def pick(body):
        return {**_pick(body), "origin": ("person", 0.75)} if "Pia skriver" in body["state"].get("text", "") \
            else _pick(body)
    bf, _ = _run(conn, "person", pick)
    pia = ids["pia"]
    backfill.accept(conn, bf["run_id"], TWO)
    assert _rows(conn, bf["run_id"], "origin")[(pia[0], "origin", "person")]["status"] == "active"
    _gate(conn, "sender_kind")
    res = focus.run(conn, "sender_kind", "uncertain", client=client(FakeJev(pick=_sender_pick("machine", 0.95))),
                    progress=lambda s: None)
    assert res["stored"] == 1 and res["propagation"]["superseded"] == len(pia)
    old = _rows(conn, bf["run_id"], "sender_kind")
    assert {r["status"] for (e, f, v), r in old.items() if e in pia} == {"superseded"}
    assert {r["decided_by"] for (e, f, v), r in old.items() if e in pia} == {f"superseded:{res['run_id']}"}
    new = _rows(conn, res["run_id"], "sender_kind")
    assert {e for (e, f, v) in new} == set(pia)
    # accepted over the chain: the newest answer is the boundary, and origin person is held back
    out = backfill.accept(conn, backfill.runs_of(conn, "all"), TWO)
    assert _eff(conn, pia[1], "sender_kind") == ("machine", "model")
    assert _rows(conn, bf["run_id"], "origin")[(pia[1], "origin", "person")]["status"] == "proposed"
    assert out["fields"]["origin"]["held_back"] == len(pia)
    assert out["fields"]["sender_kind"]["promoted"] == len(pia)
    # a rerun of the older run's proposals does not bring its superseded answer back
    backfill.propagate(conn, bf["run_id"])
    assert {r["status"] for (e, f, v), r in _rows(conn, bf["run_id"], "sender_kind").items() if e in pia} == \
        {"superseded"}
    ranked = conn.execute("select count(*) as n from effective_message_assignment where message_id = any(%s)"
                          " and dimension_id = 'sender_kind'", (pia,)).fetchone()["n"]
    assert ranked == len(pia)                                           # one value each, deterministic


def test_focused_dry_runs_send_nothing_read_no_key_and_write_nothing(conn, ingestor, database, keychain):
    ids = _archive(conn, ingestor)
    _run(conn, "machine")
    _run(conn, "person")
    tables = ("model_run", "enrich_case", "enrich_prediction", "assignment", "jev_case", "jev_prediction")
    before = {t: conn.execute(f"select count(*)::int as n from {t}").fetchone()["n"] for t in tables}
    conn.commit()
    keychain.clear()  # the backfill runs above read the (fake) key
    fake = FakeJev(pick=_sender_pick("machine", 0.9))
    with db.connect(database, autocommit=True) as ac:
        ac.execute("set default_transaction_read_only = on")  # as on the real archive
        res = focus.run(ac, "value", "all", client=client(fake), dry_run=True)
    assert fake.requests == [] and keychain == []
    assert {t: conn.execute(f"select count(*)::int as n from {t}").fetchone()["n"] for t in tables} == before
    e = res["estimate"]
    assert res["to_send"] > 0 and 0 < e["input_tokens_per_case"] < e["full_tokens_per_case"] / 2
    assert res["request"]["questions"].keys() == {"value"} and res["gold_run"] is None
    text = focus.format_dry_run(res)
    assert text.startswith("DRY RUN") and "a real run is refused until" in text
    assert json.loads(text[text.index("\n{") + 1:]) == res["request"]
    with pytest.raises(focus.FocusError, match="run this question on the answer key first"):
        focus.run(conn, "value", "all", client=client(fake))
    assert fake.requests == [] and keychain == []
    with pytest.raises(focus.FocusError, match="fixed on Teams"):
        focus.select(conn, "origin", "all", stages=["teams"])
    del ids


def test_the_focused_question_on_the_answer_key_reports_its_accuracy_beside_the_full_set(conn, ingestor, keychain):
    ids = _archive(conn, ingestor)
    _gold_run(conn, ids)
    fake = FakeJev(pick=_sender_pick("people", 0.97))
    dry = focus.gold_run(conn, 1, "sender_kind", client=client(fake), dry_run=True)
    assert fake.requests == [] and dry["to_send"] == 3 and dry["input_tokens_per_case"] < dry["full_tokens_per_case"]
    res = focus.gold_run(conn, 1, "sender_kind", client=client(fake), progress=lambda s: None)
    assert len(fake.requests) == 3 and res["reference_run"] == "g1"
    acc = res["accuracy"]
    assert acc["items"] == 3
    assert acc["runs"][res["run_id"]] == {**acc["runs"][res["run_id"]], "decided": 3, "right": 3}
    assert (acc["runs"]["g1"]["decided"], acc["runs"]["g1"]["right"]) == (2, 1)
    assert focus.gold_runs(conn, "sender_kind", res["question_version"])[0]["id"] == res["run_id"]
    text = focus.format_gold(res)
    assert "focused" in text and "full set" in text
    # the gate is open: the same question may now run on the archive
    _run(conn, "person")
    assert focus.run(conn, "sender_kind", "all", dry_run=True)["gold_run"] == res["run_id"]


def test_a_combined_run_asks_each_unit_once_with_only_the_fields_it_is_unsure_of(conn, ingestor, keychain):
    ids = _archive(conn, ingestor)

    def pick(body):  # the invoices' origin at 0.5 (origin and its sender side unsure); Pia's value at 0.6
        return {**_pick(body), "origin": ("notification", 0.5)} if "Faktura nr" in body["state"].get("text", "") \
            else _pick(body)
    _run(conn, "machine", pick)
    _run(conn, "person")
    fields = ["sender_kind", "origin", "value"]
    sel = focus.select_many(conn, fields, "uncertain")
    separate = sum(len(focus.select(conn, f, "uncertain")["cases"]) for f in fields)
    by_anchor = {c.anchor: sel["unit_fields"][(c.stage, c.key)] for c in sel["cases"]}
    assert by_anchor == {ids["invoices"][0]: ("sender_kind", "origin"), ids["invoices"][2]: ("sender_kind", "origin"),
                         ids["invoices"][4]: ("sender_kind", "origin"), ids["pia"][-1]: ("value",)}
    assert len(sel["cases"]) == 4 < separate == 7                        # the invoices once, not twice
    fake = FakeJev(pick=lambda b: {"sender_kind": ("machine", 0.96), "origin": ("notification", 0.95),
                                   "value": ("context", 0.95)}, input_tokens=1200)
    with pytest.raises(focus.FocusError, match="ask the answer key first, for sender_kind, origin, value"):
        focus.run_many(conn, fields, "uncertain", client=client(fake))
    assert fake.requests == []
    for f in fields:
        _gate(conn, f)
    dry = focus.run_many(conn, fields, "uncertain", dry_run=True)
    assert dry["unchecked"] == [] and dry["to_send"] == 4 and dry["estimate"]["asks"] == 7
    assert dry["estimate"]["input_tokens_per_case"] < dry["estimate"]["separate_tokens_per_case"]
    assert "separately 7 cases; combined 4 cases" in focus.format_dry_run_many(dry)
    res = focus.run_many(conn, fields, "uncertain", client=client(fake), progress=lambda s: None)
    assert res["stored"] == 4 and len(fake.requests) == 4
    asked = sorted(tuple(sorted(b["questions"])) for b in fake.requests)
    assert asked == [("origin", "sender_kind")] * 3 + [("value",)]
    run = conn.execute("select * from model_run where id = %s", (res["run_id"],)).fetchone()
    assert run["purpose"] == "enrich-focus" and run["params"]["focus"]["fields"] == fields
    stored = {r["anchor_id"]: r["fs"] for r in conn.execute(
        "select c.anchor_id, array_agg(p.field order by p.field) as fs from enrich_case c"
        " join enrich_prediction p on p.case_id = c.id where c.run_id = %s group by 1", (res["run_id"],))}
    assert stored[ids["pia"][-1]] == ["value"] and stored[ids["invoices"][0]] == ["origin", "sender_kind"]
    rows = _rows(conn, res["run_id"])
    assert {e for (e, f, v) in rows if f == "sender_kind"} == set(ids["invoices"])   # asked, not derived
    assert rows[(ids["invoices"][1], "sender_kind", "machine")]["confidence"] == pytest.approx(0.96)
    assert {e for (e, f, v) in rows if f == "value" and v == "context"} >= {ids["pia"][-1]}
    for f in fields:  # each field's newest answer is the combined run's: nothing unsure is left
        assert focus.select(conn, f, "uncertain")["cases"] == []


def test_a_combined_answer_key_run_opens_the_gate_for_each_of_its_fields(conn, ingestor, keychain):
    ids = _archive(conn, ingestor)
    _gold_run(conn, ids)
    fake = FakeJev(pick=lambda b: {"sender_kind": ("people", 0.97), "value": ("context", 0.9)})
    res = focus.gold_run(conn, 1, ["sender_kind", "keep"], client=client(fake), progress=lambda s: None)
    assert res["fields"] == ["sender_kind", "value"]                     # keep is asked as value
    assert all(set(b["questions"]) == {"sender_kind", "value"} for b in fake.requests) and len(fake.requests) == 3
    assert set(res["accuracies"]) == {"sender_kind", "value"}
    assert res["accuracies"]["sender_kind"]["runs"][res["run_id"]]["right"] == 3
    for f in ("sender_kind", "value"):
        qv = focus.question_version(focus.question_sets(focus.dims_for(conn, f), f))
        assert focus.gold_runs(conn, f, qv)[0]["id"] == res["run_id"]
    assert "combined" in focus.format_gold(res)
    _run(conn, "person")
    assert focus.run_many(conn, ["sender_kind", "keep"], "all", dry_run=True)["unchecked"] == []
    assert focus.run(conn, "sender_kind", "all", dry_run=True)["gold_run"] == res["run_id"]
    with pytest.raises(focus.FocusError, match="one or more of"):
        focus.parse_fields("kind,sphere")


def _kind_pick(value, p):
    def pick(body):
        assert list(body["questions"]) == ["kind"], "one question per case"
        return {"kind": (value, p)}
    return pick


def test_where_the_direct_kind_stays_unsure_accept_brings_back_the_kind_worked_out_from_type(conn, ingestor, keychain):
    ids = _archive(conn, ingestor)
    inv = ids["invoices"][0]
    bf, _ = _run(conn, "machine")
    derived = conn.execute("select value, confidence from assignment where source_ref = %s and dimension_id = 'kind'"
                           " and entity_id = %s", (bf["run_id"], inv)).fetchone()
    assert derived["value"] == "transaction" and derived["confidence"] >= 0.85          # from type invoice 0.93
    _gate(conn, "kind")
    unsure = focus.run(conn, "kind", "all", stages=["machine"], client=client(FakeJev(pick=_kind_pick("fyi", 0.6))),
                       progress=lambda s: None)
    out = backfill.accept(conn, backfill.runs_of(conn, "all"), TWO)
    assert _eff(conn, inv, "kind") == ("transaction", "model")                         # not the unsure fyi
    row = conn.execute("select decided_by, evidence, source_ref from assignment where entity_id = %s"
                       " and dimension_id = 'kind' and status = 'active'", (inv,)).fetchone()
    assert row["decided_by"] == "kind-from-type:kind>=0.85" and row["source_ref"] == bf["run_id"]
    assert row["evidence"]["superseded_by"] == f"superseded:{unsure['run_id']}"
    assert out["kind_from_type"]["brought_back"] >= 1 and out["kind_from_type"]["values"]["transaction"] >= 1
    assert "kind from type ≥ 0.85" in backfill.format_accept(out)
    # accepting again changes nothing; unaccept puts it back as it was
    again = backfill.accept(conn, backfill.runs_of(conn, "all"), TWO)
    assert again["kind_from_type"]["brought_back"] == 0 and _eff(conn, inv, "kind") == ("transaction", "model")
    backfill.unaccept(conn, backfill.runs_of(conn, "all"))
    back = conn.execute("select status, decided_by, evidence from assignment where source_ref = %s and dimension_id = 'kind'"
                        " and entity_id = %s", (bf["run_id"], inv)).fetchone()
    assert back["status"] == "superseded" and back["decided_by"] == f"superseded:{unsure['run_id']}"
    assert "superseded_by" not in back["evidence"]
    # a direct kind that is sure wins: the newer run supersedes the kind brought back
    backfill.accept(conn, backfill.runs_of(conn, "all"), TWO)
    focus.run(conn, "kind", "all", stages=["machine"], client=client(FakeJev(pick=_kind_pick("fyi", 0.95))),
              run_id="jev-focus-kind-sure", progress=lambda s: None)
    backfill.accept(conn, backfill.runs_of(conn, "all"), TWO)
    assert _eff(conn, inv, "kind") == ("fyi", "model")


def test_the_command_line_accepts_two_level_over_all_runs_and_shows_a_focused_dry_run(
        conn, ingestor, database, vault, monkeypatch, capsys, keychain):
    _archive(conn, ingestor)
    runs = [_run(conn, st)[0]["run_id"] for st in ("machine", "person")]
    monkeypatch.setenv("TALOS_DSN", database)
    monkeypatch.setenv("TALOS_HOME", str(vault.root.parent))
    monkeypatch.setattr(cli, "_logging", lambda settings, verbose: None)
    cli.main(["enrich", "accept", "--run", "all", "--two-level", "--dry-run"])
    out = capsys.readouterr().out
    assert out.startswith("DRY RUN (rolled back): 2 runs") and "sender_kind ≥ 0.90" in out and "origin  ≥ 0.70" in out
    cli.main(["enrich", "accept", "--run", ",".join(runs), "--two-level", "--keep", "0.95"])
    out = capsys.readouterr().out
    assert "keep    ≥ 0.95" in out and "keep>=0.95" in out
    cli.main(["enrich", "unaccept", "--run", "all"])
    assert "back to proposed" in capsys.readouterr().out
    cli.main(["enrich", "jev", "focus", "--field", "sender_kind", "--where", "all", "--dry-run"])
    out = capsys.readouterr().out
    assert out.startswith("DRY RUN") and "for this one question" in out
    cli.main(["enrich", "propagate", "--run", runs[0], "--fields", "boundaries"])
    assert "proposals written" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["enrich", "jev", "focus", "--field", "sender_kind"])
    assert "usage: talos enrich jev focus" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["enrich", "accept", "--run", "nope"])
    assert "no enrichment backfill run" in capsys.readouterr().err


def test_the_page_scores_a_threshold_as_the_server_does(tmp_path):
    """accScore in app.js and acceptance.score() give the same numbers (the page recomputes them
    on every slider move)."""
    import re
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    src = (Path(__file__).parent.parent / "src" / "talos" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    fn = re.search(r"^function accScore\(.*?^}\n", src, re.S | re.MULTILINE).group(0)
    cases = [{"p": "person", "r": "person", "c": 0.97, "m": 0.9, "ps": "people", "rs": "people", "bs": "people", "bp": 0.98},
             {"p": "notification", "r": "person", "c": 0.95, "m": 0.9, "ps": "machine", "rs": "people", "bs": "machine",
              "bp": 0.97},
             {"p": "person", "r": "person", "c": 0.72, "m": 0.1, "ps": "people", "rs": "people", "bs": "people", "bp": 0.78},
             {"p": "person", "r": "list", "c": 0.75, "m": 0.6, "ps": "people", "rs": "people", "bs": "machine", "bp": 0.91},
             {"p": "alert", "r": "notification", "c": 0.7, "m": 0.2, "ps": "machine", "rs": "machine", "bs": None, "bp": 0}]
    (tmp_path / "t.js").write_text(fn + f"const cases = {json.dumps(cases)};\n"
                                   "const out = [];\n"
                                   "for (const [t, m, tb] of [[0.7, 0.15, 0.9], [0.7, null, null], [0.96, 0.15, 0.9], [0.5, 0.15, 0.95]]) {\n"
                                   "  const r = accScore(cases, t, m, tb);\n"
                                   "  out.push([r.decided, r.right, r.costly, r.confusions]); }\n"
                                   "console.log(JSON.stringify(out));\n", encoding="utf-8")
    got = json.loads(subprocess.run([node, str(tmp_path / "t.js")], capture_output=True, text=True, check=True).stdout)
    want = []
    for t, m, tb in ((0.7, 0.15, 0.9), (0.7, None, None), (0.96, 0.15, 0.9), (0.5, 0.15, 0.95)):
        r = acceptance.score(cases, t, margin=m, boundary_threshold=tb)
        want.append([r["decided"], r["right"], r["costly"], [list(x) for x in r["confusions"].items()]])
    assert got == want
    assert want[0][:3] == [3, 1, 1]   # the list-for-person case is held back: its boundary says machine
