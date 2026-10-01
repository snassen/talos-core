"""Kind: the coarse level above type (docs/mailbox-structure-plan.md, decisions 7 and 8).

The taxonomy's mapping from type to kind, kind derived from Jev's full type scores and accepted
two-level, the direct kind question, answer keys that label their own fields, set 3's sampler
(sender groups, shared senders split by system, earlier sets left out), the multi-labeller report
and giving the owner's answer to a sender group.

Never the real Jev: every request goes to test_jev's fake endpoint. Invented mail only.
"""

import json

import pytest
from test_backfill import _archive, _rows
from test_accept import TWO, _eff, _gate, _run
from test_enrich import mail
from test_gold import POST, _teams
from test_jev import FakeJev, keychain  # noqa: F401  (keychain is a fixture)
from test_backfill import client as jev_client
from test_web import client

from talos import backfill, boundary, focus, gold, personal, rules, search, taxonomy

pytestmark = pytest.mark.usefixtures("taxonomy_loaded")

DATA = json.loads(taxonomy.DEFAULT_PATH.read_text(encoding="utf-8"))
KINDS = [v["value"] for v in DATA["dimensions"]["kind"]["values"]]
TYPES = [v["value"] for v in DATA["dimensions"]["type"]["values"]]
SET3 = ["origin", "kind", "topic", "ask", "value", "route"]


def _file(tmp_path, data=None, text=None):
    path = tmp_path / "taxonomy.json"
    path.write_text(text if text is not None else json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------- the mapping

def test_every_type_is_under_exactly_one_kind_in_the_file_and_the_load_stores_the_mapping(conn):
    groups = DATA["dimensions"]["kind"]["derived_from"]["values"]
    flat = [t for g in groups.values() for t in g]
    assert sorted(flat) == sorted(TYPES) and len(flat) == len(set(flat))
    assert KINDS == ["conversation", "question_request", "invitation", "fyi", "announcement", "alert", "report",
                     "offer", "transaction", "spam", "other"]
    side_of = boundary.sides(conn)["kind"]["side_of"]
    # the owner's set 2: a renewal, a resolved ticket and a plug-in update are FYI; a reply is a conversation
    assert [side_of[t] for t in ("subscription", "ticket", "service_update", "reply_thanks", "change_notice")] == \
        ["fyi", "fyi", "fyi", "conversation", "fyi"]
    assert side_of["newsletter"] == side_of["event_webinar"] == "announcement" and side_of["shipping"] == "transaction"
    stored = conn.execute("select derived_from from dimension where id = 'kind'").fetchone()["derived_from"]
    assert stored["dimension"] == "type" and stored["values"]["report"] == ["backup", "report"]


@pytest.mark.parametrize("change, message", [
    (lambda g: g["fyi"].append("invoice"), "under more than one kind: invoice (fyi, transaction)"),
    (lambda g: g["transaction"].remove("shipping"), "these type values map to no kind: shipping"),
    (lambda g: g["fyi"].append("fax"), "the file does not have: fax"),
    (lambda g: g.__setitem__("misc", ["other"]), "unknown value or a group that is not a list: misc"),
])
def test_a_type_under_two_kinds_under_none_or_an_unknown_one_refuses_the_whole_file(conn, tmp_path, change, message):
    data = json.loads(json.dumps(DATA))
    change(data["dimensions"]["kind"]["derived_from"]["values"])
    before = conn.execute("select derived_from from dimension where id = 'kind'").fetchone()["derived_from"]
    with pytest.raises(taxonomy.TaxonomyError, match=message.replace("(", r"\(").replace(")", r"\)")):
        taxonomy.load(conn, _file(tmp_path, data))
    assert conn.execute("select derived_from from dimension where id = 'kind'").fetchone()["derived_from"] == before


def test_a_key_given_twice_in_the_file_is_refused_not_quietly_dropped(conn, tmp_path):
    text = taxonomy.DEFAULT_PATH.read_text(encoding="utf-8")
    twice = text.replace('"report": [\n            "backup",', '"fyi": ["backup"],\n          "report": [\n            "backup",', 1)
    assert twice != text
    with pytest.raises(taxonomy.TaxonomyError, match="a key is given twice in one object: fyi"):
        taxonomy.load(conn, _file(tmp_path, text=twice))


# ---------------------------------------------------------------- derived from the full scores, accepted, undone

def test_kind_is_the_sum_of_the_full_probabilities_of_the_types_under_it(conn, ingestor, keychain):
    ids = _archive(conn, ingestor)
    res, _ = _run(conn, "machine")
    row = _rows(conn, res["run_id"], "kind")[(ids["invoices"][1], "kind", "transaction")]
    # invoice 0.93, the 63 other types (0.07 / 63) each: six of them are transactions too
    assert row["confidence"] == pytest.approx(0.93 + 6 * 0.07 / 63, abs=1e-4)
    ev = row["evidence"]
    assert ev["boundary_of"] == "type" and ev["decided"] is True and ev["propagated"] is True
    assert set(ev["scores"]) == set(KINDS) and sum(ev["scores"].values()) == pytest.approx(1.0, abs=1e-3)
    assert ev["margin"] == pytest.approx(row["confidence"] - max(p for k, p in ev["scores"].items()
                                                                 if k != "transaction"), abs=1e-3)
    top3 = _rows(conn, res["run_id"], "type")[(ids["invoices"][1], "type", "invoice")]["evidence"]["scores"]
    assert sum(top3.values()) < row["confidence"]  # the top three would have said less
    pia, _ = _run(conn, "person")
    q = _rows(conn, pia["run_id"], "kind")[(ids["pia"][0], "kind", "question_request")]
    assert q["confidence"] == pytest.approx(0.8 + 5 * 0.2 / 63, abs=1e-4) and q["evidence"]["decided"] is False


def test_two_level_accepts_kind_at_085_holds_back_a_type_of_another_kind_and_unaccept_undoes_it(
        conn, ingestor, keychain):
    ids = _archive(conn, ingestor)
    runs = [_run(conn, st)[0]["run_id"] for st in backfill.STAGES]
    inv, pia = ids["invoices"], ids["pia"][-1]
    assert TWO["kind"] == boundary.KIND_MIN == 0.85
    out = backfill.accept(conn, runs, TWO)
    assert out["fields"]["kind"]["promoted"] > 0 and "kind>=0.85" in out["decided_by"]
    assert _eff(conn, inv[1], "kind") == ("transaction", "model")
    assert _eff(conn, pia, "kind") is None                 # 0.82: under the line, stays a proposal
    assert _eff(conn, pia, "type") == ("request", "model")  # the type itself is accepted at 0.70
    # the owner's kind on one invoice: its type (a transaction) is held back there, and only there
    rules.assign(conn, [inv[1]], "kind", "fyi")
    out = backfill.accept(conn, runs, TWO)
    t = _rows(conn, runs[0], "type")
    assert t[(inv[1], "type", "invoice")]["status"] == "proposed" and out["fields"]["type"]["held_back"] == 1
    assert t[(inv[3], "type", "invoice")]["status"] == "active"
    # a run from before kind gets its kind proposals derived first
    conn.execute("delete from assignment where source_ref = %s and dimension_id = 'kind'", (runs[0],))
    res = backfill.accept(conn, runs[0], {"kind": 0.85}, dry_run=True)
    assert res["boundaries_written"][runs[0]] > 0 and res["fields"]["kind"]["promoted"] > 0
    assert not _rows(conn, runs[0], "kind")                # a dry run: rolled back
    backfill.propagate(conn, runs[0], fields=["kind"])
    undone = backfill.unaccept(conn, runs)
    assert undone["fields"]["kind"] > 0
    assert {r["status"] for rid in runs for r in _rows(conn, rid, "kind").values()} == {"proposed"}
    assert _eff(conn, inv[3], "kind") is None and _eff(conn, inv[1], "kind") == ("fyi", "human")


def test_kind_filters_messages_and_is_a_facet_by_the_effective_value(conn, ingestor, vault, database, keychain):
    ids = _archive(conn, ingestor)
    runs = [_run(conn, st)[0]["run_id"] for st in backfill.STAGES]
    backfill.accept(conn, runs, {"kind": 0.85})
    rules.assign(conn, [ids["invoices"][0]], "kind", "fyi")
    conn.commit()
    got = {r["id"] for r in search.messages(conn, dimension=("kind", "transaction"), limit=100)["rows"]}
    assert got == set(ids["invoices"][1:])
    c = client(database, vault)
    rows = c.get("/api/messages", params={"dim": "kind:fyi", "limit": 100}).json()["rows"]
    assert [r["id"] for r in rows] == [ids["invoices"][0]]
    kinds = {r["value"]: r["n"] for r in c.get("/api/facets").json()["kind"]}
    assert kinds["transaction"] == 4 and kinds["fyi"] == 1


# ---------------------------------------------------------------- kind asked directly

def test_the_direct_kind_question_is_one_choice_over_the_kinds_with_their_definitions(conn):
    qs = focus.question_sets(focus.dims_for(conn, "kind"), "kind")
    email, teams = qs["email"], qs["teams"]
    assert list(email) == ["kind"] and list(teams) == ["kind"]
    q = email["kind"]
    assert q["type"] == "choice" and "this message" in q["instructions"] and "in one coarse word" in q["instructions"]
    assert list(q["criteria"]) == KINDS
    by = {v["value"]: v for v in DATA["dimensions"]["kind"]["values"]}
    assert q["criteria"]["fyi"] == by["fyi"]["description"]
    assert q["criteria"]["question_request"].startswith("Question or request: ")
    assert list(teams["kind"]["criteria"]) == KINDS and "this chat window" in teams["kind"]["instructions"]
    assert "kind" in focus.FIELDS and "kind" not in focus.NO_TEAMS and focus.THRESHOLDS["kind"] == 0.85


def test_a_focused_kind_run_stores_kind_itself_and_its_answer_is_the_one_proposed(conn, ingestor, keychain):
    ids = _archive(conn, ingestor)
    runs = [_run(conn, st)[0]["run_id"] for st in ("machine", "person")]
    _gate(conn, "kind")
    fake = FakeJev(pick=lambda body: {"kind": ("fyi", 0.9)})
    res = focus.run(conn, "kind", "all", stages=["machine"], client=jev_client(fake), progress=lambda s: None)
    assert all(list(r["questions"]) == ["kind"] for r in fake.requests)
    assert conn.execute("select count(*) as n from enrich_prediction p join enrich_case c on c.id = p.case_id"
                        " where c.run_id = %s and p.field = 'kind'", (res["run_id"],)).fetchone()["n"] == res["stored"]
    new = _rows(conn, res["run_id"], "kind")
    assert new[(ids["invoices"][1], "kind", "fyi")]["status"] == "proposed"
    assert "boundary_of" not in new[(ids["invoices"][1], "kind", "fyi")]["evidence"]   # asked, not derived
    old = _rows(conn, runs[0], "kind")[(ids["invoices"][1], "kind", "transaction")]
    assert old["status"] == "superseded"


# ---------------------------------------------------------------- set 3: uncertain cases, sender groups

def _case(conn, run, stage, kind, key, anchor, members, *, field="type", top="fyi", p=0.5):
    cid = conn.execute("insert into enrich_case (run_id, stage, unit_kind, unit_key, anchor_id, member_ids)"
                       " values (%s, %s, %s, %s, %s, %s) returning id", (run, stage, kind, key, anchor, members)).fetchone()["id"]
    rest = (1 - p) / (len(TYPES) - 1)
    scores = {t: (p if t == top else rest) for t in TYPES} if field == "type" else {top: p}
    conn.execute("insert into enrich_prediction (case_id, field, top, selected, scores, confidence, margin, decided)"
                 " values (%s, %s, %s, '{}', %s, %s, %s, false)", (cid, field, top, json.dumps(scores), p, p - rest))
    conn.execute("insert into enrich_prediction (case_id, field, top, selected, scores, confidence, margin, decided)"
                 " values (%s, 'ask', 'none', '{}', '{\"none\": 0.9}', 0.9, 0.8, true)", (cid,))
    return cid


def _unsure_archive(conn, ingestor) -> dict:
    """Backfill cases Jev was unsure of: itrobot@ (a shared sender) with six [Success], six [Failed]
    and two QNAP mails; a newsletter with seven different subjects; three people; five windows."""
    for r in ("jev-backfill-machine-t", "jev-backfill-person-t", "jev-backfill-teams-t"):
        conn.execute("insert into model_run (id, model, purpose, params) values (%s, 'jev', 'enrich-backfill', '{}')", (r,))
    ids: dict = {"success": [], "failed": [], "qnap": [], "news": [], "people": []}
    robot = "IT Robot <itrobot@company.example>"
    words = ["alfa", "bravo", "charlie", "delta", "echo", "foxtrot", "golf"]
    for i in range(6):
        ids["success"].append(mail(ingestor, frm=robot, subject=f"[Success] Job {words[i]}", days_ago=10 + i))
        ids["failed"].append(mail(ingestor, frm=robot, subject=f"[Failed] Job {words[i]}", days_ago=20 + i))
    for i in range(2):
        ids["qnap"].append(mail(ingestor, frm=robot, subject=f"QNAP - disk {words[i]}", days_ago=30 + i))
    for i, s in enumerate(["Höstrea nu", "Nya jackor", "Sista chansen", "Veckans tips", "Julklappar", "Fri frakt",
                           "Medlemsdagar"]):
        ids["news"].append(mail(ingestor, frm="Butiken <news@butiken.example>", subject=s, days_ago=40 + i))
    for i in range(3):
        ids["people"].append(mail(ingestor, frm=f"Vän {i} <van{i}@example.org>", subject=f"Hej {i}", thread=f"v{i}"))
    for key in ("success", "failed", "qnap", "news"):
        for m in ids[key]:
            _case(conn, "jev-backfill-machine-t", "machine", "tail", f"tail:{m}", m, [m])
    for m in ids["people"]:
        _case(conn, "jev-backfill-person-t", "person", "thread", f"message:{m}", m, [m], field="origin", top="person")
    ids["windows"] = []
    for w in range(5):
        chat = _teams(conn, ingestor, f"chat{w}", [0, 1])
        ids["windows"].append(chat)
        _case(conn, "jev-backfill-teams-t", "teams", "window", f"window:{w}", chat[-1], chat, field="topic",
              top="Work/Company")
    conn.commit()
    return ids


@pytest.mark.parametrize("subject, head", [
    ("[nas.company.example] Disk full", "[nas.company.example]"), ("[Success] Job A (1 VM)", "[Success]"),
    ("[Failed] Job A", "[Failed]"), ("[backup5.company.example]Active Backup task", "[backup5.company.example]Active"),
    ("[backup5.company.example]Network backup", "[backup5.company.example]Network"),
    ("dc1 Down!", "… Down!"), ("192.0.2.41 Down!", "… Down!"), ("grafana1 Up!", "… Up!"),
    ("QNAP - disk", "QNAP"), ("Value Error on Smart-UPS", "Value"), ("Fw: [Success] Job", "[Success]"),
    ("[JIRA-12345] Ticket", "[JIRA-#]"), ("2024 report", "####"), (None, "(no subject)")])
def test_a_shared_senders_system_is_its_tag_else_its_status_word_else_its_first_word(subject, head):
    assert gold.system_key(subject) == head


def test_the_sampler_draws_sender_groups_split_by_system_and_never_an_earlier_sets_message(conn, ingestor):
    ids = _unsure_archive(conn, ingestor)
    # an earlier answer key holds one newsletter: its case is never drawn, nor counted in a group
    conn.execute("insert into gold_set (id, name, seed, target) values (2, 'set 2', 26, 1)")
    conn.execute("insert into gold_item (set_id, position, message_id, stratum, reason, unit)"
                 " values (2, 1, %s, 'machine', 'r', 'message')", (ids["news"][0],))
    conn.commit()
    units = gold.uncertain_units(gold.uncertain_cases(conn, exclude={ids["news"][0]}))
    groups = {(g["sender"], g["system"]): g for g in units["group"]}
    assert set(groups) == {("itrobot@company.example", "[Success]"), ("itrobot@company.example", "[Failed]"),
                           ("news@butiken.example", None)}      # a newsletter's subjects never split it
    assert groups[("news@butiken.example", None)]["messages"] == 6
    assert units["split"]["itrobot@company.example"] == {"heads": 3, "cases": 14, "groups": 2, "grouped_cases": 12}
    with pytest.raises(gold.GoldError, match="seed 26 was used"):
        gold.sample_uncertain(conn, n=15, seed=26, fields=SET3)
    dry = gold.sample_uncertain(conn, n=15, seed=7, fields=SET3, dry_run=True)
    assert dry == gold.sample_uncertain(conn, n=15, seed=7, fields=SET3, dry_run=True)   # the same seed, the same draw
    res = gold.sample_uncertain(conn, n=15, seed=7, fields=SET3)
    assert res["want"] == {"group": 6, "single": 5, "teams": 4} and len(res["groups"]) == 3  # all there are
    # three groups, the five singles and four windows, and the short groups' share: the last window
    assert res["items"] == 13 and conn.execute("select count(*) as n from gold_set").fetchone()["n"] == 2
    assert res["anchors"] == dry["anchors"]
    sid = res["set_id"]
    assert gold.set_fields(conn, sid) == SET3
    items = conn.execute("select * from gold_item where set_id = %s order by position", (sid,)).fetchall()
    assert ids["news"][0] not in {i["message_id"] for i in items}
    grouped = [i for i in items if i["info"].get("sender_group")]
    assert len(grouped) == 3 and {i["info"]["sender_group"]["system"] for i in grouped} == {"[Success]", "[Failed]", None}
    for i in grouped:
        cases = conn.execute("select count(*) as n from gold_group_case where item_id = %s", (i["id"],)).fetchone()["n"]
        assert cases == i["info"]["sender_group"]["cases"] == 6
    assert sum(1 for i in items if i["stratum"] == "teams") == 5


# ---------------------------------------------------------------- the screen: the set's fields, the group, mixed

def _set3(conn, ingestor):
    ids = _unsure_archive(conn, ingestor)
    sid = gold.sample_uncertain(conn, n=15, seed=11, fields=SET3)["set_id"]
    conn.commit()
    items = {i["position"]: i for i in conn.execute("select * from gold_item where set_id = %s", (sid,)).fetchall()}
    group = next(p for p, i in items.items() if (i["info"].get("sender_group") or {}).get("system") == "[Failed]")
    single = next(p for p, i in items.items() if not i["info"].get("sender_group"))
    return ids, sid, items, group, single


KIND_ANSWER = {"origin": ["notification"], "kind": ["alert"], "topic": ["Work/Backup"], "ask": [], "value": ["transient"],
               "route": []}


def test_a_set_labels_its_own_fields_on_the_screen_in_progress_and_in_the_list(conn, ingestor, vault, database):
    _, sid, items, group, single = _set3(conn, ingestor)
    c = client(database, vault)
    it = c.get(f"/api/gold/{sid}/items/{single}").json()
    assert it["fields"] == SET3 and it["group"] is None
    meta = c.get(f"/api/gold/{sid}").json()
    assert meta["progress"]["label_fields"] == SET3 and list(meta["progress"]["fields"]) == SET3
    assert [o["value"] for o in meta["options"]["kind"]] == KINDS
    # type is not a field of this set: refused; kind is
    r = c.post(f"/api/gold/{sid}/items/{single}/labels", headers=POST, json={"field": "type", "values": ["invoice"]})
    assert r.status_code == 400 and "does not label type" in r.json()["error"]
    for f, v in KIND_ANSWER.items():
        res = c.post(f"/api/gold/{sid}/items/{single}/labels", headers=POST, json={"field": f, "values": v}).json()
    assert res["progress"]["done"] == 1                    # six of the set's fields, no type: done
    assert next(s for s in c.get("/api/gold").json()["sets"] if s["id"] == sid)["done"] == 1
    assert c.get(f"/api/gold/{sid}/items/{single}").json()["round_items"][(single - 1) % 6]["done"] is True
    # a set of the six by default still needs type
    assert gold.set_fields(conn, sid) == SET3 and gold._fields_of({}) == list(gold.FIELDS)
    # "mixed" is for a sender-group item only
    r = c.post(f"/api/gold/{sid}/items/{single}/labels", headers=POST, json={"field": "mixed", "values": ["mixed"]})
    assert r.status_code == 400 and "sender-group" in r.json()["error"]


def test_a_sender_group_item_shows_its_size_and_subjects_blind_and_takes_the_mixed_tick(conn, ingestor, vault,
                                                                                       database):
    ids, sid, items, group, _ = _set3(conn, ingestor)
    conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, status, confidence)"
                 " select id, 'kind', 'report', 'model', 'jev-backfill-machine-t', 'proposed', 0.613 from message")
    conn.commit()
    c = client(database, vault)
    r = c.get(f"/api/gold/{sid}/items/{group}")
    g = r.json()["group"]
    assert g["messages"] == 6 and g["cases"] == 6 and g["sender"] == "itrobot@company.example" and g["system"] == "[Failed]"
    anchor = items[group]["message_id"]
    subjects = [x["subject"] for x in g["examples"]]
    assert 5 <= len(subjects) <= gold.EXAMPLES_MAX and all(s.startswith("[Failed] Job ") for s in subjects)
    dates = [x["received_at"] for x in g["examples"]]
    assert dates == sorted(dates, reverse=True)
    assert anchor in ids["failed"]
    for word in ("jev-backfill", "0.613", '"report"', "unsure", "case_id", "sender group", '"info"', "stratum"):
        assert word not in r.text, word
    on = c.post(f"/api/gold/{sid}/items/{group}/labels", headers=POST, json={"field": "mixed", "values": ["mixed"]})
    assert on.json()["labels"]["mixed"] == {"values": ["mixed"], "status": "set"}
    assert on.json()["progress"]["done"] == 0              # the tick is not a field
    off = c.post(f"/api/gold/{sid}/items/{group}/labels", headers=POST, json={"field": "mixed", "values": []})
    assert "mixed" not in off.json()["labels"]


def test_the_import_asks_for_the_sets_fields_and_refuses_any_other(conn, ingestor, tmp_path):
    _, sid, items, _, _ = _set3(conn, ingestor)
    line = {"origin": "notification", "kind": "alert", "topic": "Work/Backup", "ask": [], "value": "transient",
            "route": [], "unsure": ["kind"]}
    path = tmp_path / "haiku.jsonl"
    path.write_text("\n".join(json.dumps({"position": p, **line}) for p in items) + "\n", encoding="utf-8")
    res = gold.import_labels(conn, sid, "haiku", [path])
    assert res["items"] == len(items) and res["unsure"] == {"kind": len(items)}
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"position": 1, **{k: v for k, v in line.items() if k != "kind"}, "type": "alert"}) + "\n")
    with pytest.raises(gold.GoldError) as exc:
        gold.import_labels(conn, sid, "haiku", [bad])
    assert "unknown key type" in str(exc.value) and "kind is missing" in str(exc.value)


# ---------------------------------------------------------------- the report: every labeller against the owner

def _g(values, status="set"):
    return {"values": values, "status": status}


def test_each_labeller_is_scored_on_their_sure_answers_only_per_field():
    owners = {1: {"kind": _g(["fyi"]), "ask": _g([])}, 2: {"kind": _g(["alert"]), "ask": _g(["question"])},
           3: {"kind": _g([], "unsure"), "ask": _g([], "skip")}, 4: {"kind": _g(["offer"]), "ask": _g([])}}
    theirs = {1: {"kind": _g(["fyi"]), "ask": _g([])},                  # both right
              2: {"kind": _g(["alert"], "unsure"), "ask": _g([])},      # kind right though unsure; ask wrong
              3: {"kind": _g(["spam"]), "ask": _g(["question"])},       # the owner was not sure / skipped: not compared
              4: {"kind": _g([], "skip")}}                              # they skipped kind, left ask out
    s = gold.score_against(owners, theirs, ["kind", "ask"])
    k, a = s["fields"]["kind"], s["fields"]["ask"]
    assert (k["sure"], k["compared"], k["right"], k["unsure"], k["rate"]) == (3, 2, 2, 1, 1.0)
    assert (a["sure"], a["compared"], a["right"], a["rate"]) == (3, 2, 1, 0.5)
    assert a["wrong"] == {"none→question": 1}
    assert s["overall"] == {"compared": 4, "right": 3, "rate": 0.75}


def test_the_report_puts_claude_haiku_and_jev_three_ways_beside_the_owner_and_counts_what_their_answers_cover(
        conn, ingestor, tmp_path, database, vault, capsys):
    _, sid, items, group, single = _set3(conn, ingestor)
    for p in items:
        for f, v in KIND_ANSWER.items():
            gold.save_label(conn, sid, p, f, v)
    gold.save_label(conn, sid, single, "kind", [], "unsure")          # the owner's not-sure kind: out for everyone
    gold.save_label(conn, sid, group, "mixed", ["mixed"])
    for who, kind in (("claude", "alert"), ("haiku", "report")):
        path = tmp_path / f"{who}.jsonl"
        path.write_text("\n".join(json.dumps({"position": p, **{k: (v if k in ("ask", "route") else v[0])
                                                                  for k, v in KIND_ANSWER.items()}, "kind": kind})
                                  for p in items) + "\n", encoding="utf-8")
        gold.import_labels(conn, sid, who, [path])
    # Jev asked kind directly on the set: fyi for every item
    conn.execute("insert into model_run (id, model, purpose, params) values ('jev-gold3-focus-kind', 'jev', 'gold-eval',"
                 " %s)", (json.dumps({"set_id": sid, "focus": {"field": "kind"}}),))
    for p, i in items.items():
        conn.execute("insert into jev_case (run_id, item_id, record_sha256, record_chars, model) values"
                     " ('jev-gold3-focus-kind', %s, 'x', 1, 'jev')", (i["id"],))
        conn.execute("insert into jev_prediction (run_id, item_id, field, top, selected, scores, confidence, margin,"
                     " decided, raw) values ('jev-gold3-focus-kind', %s, 'kind', 'fyi', '{fyi}', '{}', 0.9, 0.8,"
                     " true, '{}')", (i["id"],))
    conn.commit()
    rep = gold.report(conn, sid)
    rows = {r["labeller"]: r for r in rep["labellers"]["rows"]}
    sure = len(items) - 1
    assert rows["claude"]["fields"]["kind"]["right"] == rows["claude"]["fields"]["kind"]["compared"] == sure
    assert rows["haiku"]["fields"]["kind"]["right"] == 0 and rows["haiku"]["fields"]["topic"]["right"] == len(items)
    # the backfill's cases: only the machine ones were asked type (fyi at 0.5: kind fyi, summed or top)
    machine = sum(1 for p, i in items.items() if i["stratum"] == "machine" and p != single)
    jb = rows["jev (backfill)"]["fields"]["kind"]
    assert (jb["compared"], jb["right"]) == (machine, 0) and jb["wrong"] == {"fyi→alert": machine}
    assert rows["jev (top type's kind)"]["fields"]["kind"]["compared"] == machine
    direct = next(r for n, r in rows.items() if n.startswith("jev (kind asked directly"))
    assert direct["fields"]["kind"]["compared"] == sure and direct["fields"]["kind"]["right"] == 0
    g = rep["groups"]
    mixed = next(i for i in g["items"] if i["position"] == group)
    assert mixed["mixed"] and mixed["covers"] == 1 and g["groups"] == 3
    assert g["covers"] == sum(i["covers"] for i in g["items"]) and g["split_senders"]["itrobot@company.example"]["groups"] == 2
    text = gold.format_report(rep)
    assert "Against your labels, per field" in text and "jev (kind asked directly" in text
    assert "itrobot@company.example: 14 unsure cases, 3 heads, 2 groups" in text and "MIXED" in text


# ---------------------------------------------------------------- the owner's answer given to a sender group

def test_the_owners_answer_goes_to_every_message_of_a_group_they_did_not_tick_mixed_and_a_dry_run_writes_nothing(
        conn, ingestor):
    ids, sid, items, failed, _ = _set3(conn, ingestor)
    success = next(p for p, i in items.items() if (i["info"].get("sender_group") or {}).get("system") == "[Success]")
    news = next(p for p, i in items.items() if (i["info"].get("sender_group") or {}).get("sender") == "news@butiken.example")
    for p in (failed, success, news):
        for f, v in KIND_ANSWER.items():
            gold.save_label(conn, sid, p, f, v)
    gold.save_label(conn, sid, success, "kind", ["report"])
    gold.save_label(conn, sid, news, "mixed", ["mixed"])
    gold.save_label(conn, sid, failed, "topic", [], "unsure")          # not sure: not given
    rules.assign(conn, [ids["failed"][0]], "kind", "fyi")              # the owner's own decision on one message stays
    conn.commit()
    dry = gold.propagate_groups(conn, sid)
    assert dry["dry_run"] and dry["written"] > 0
    assert not conn.execute("select 1 from assignment where source_ref like 'gold:%'").fetchone()
    res = gold.propagate_groups(conn, sid, dry_run=False)
    assert res["written"] == dry["written"]
    by = {i["position"]: i for i in res["items"]}
    assert by[news]["mixed"] and by[news]["fields"] == {}
    assert by[failed]["fields"] == {"kind": 5, "origin": 6, "value": 6}     # topic unsure; one kept the owner's own kind
    assert by[success]["fields"]["kind"] == 6
    row = conn.execute("select * from assignment where entity_id = %s and dimension_id = 'kind'"
                       " and source_ref = %s", (ids["failed"][1], f"gold:{sid}:{failed}")).fetchone()
    assert row["source_kind"] == "human" and row["decided_by"] == personal.OWNER_ID and row["status"] == "active"
    assert row["evidence"]["propagated_from"] == {"set": sid, "item": failed, "item_id": items[failed]["id"]}
    assert row["evidence"]["sender_group"] == "itrobot@company.example | [Failed]"
    assert _eff(conn, ids["failed"][0], "kind") == ("fyi", "human")
    assert not conn.execute("select 1 from assignment where entity_id = any(%s) and source_ref like 'gold:%%'",
                            (ids["news"],)).fetchone()
    # idempotent; a changed answer supersedes what the old one gave
    assert gold.propagate_groups(conn, sid, dry_run=False)["written"] == 0
    gold.save_label(conn, sid, success, "kind", ["alert"])
    again = gold.propagate_groups(conn, sid, dry_run=False)
    assert again["superseded"] == 6 and again["items"][[i["position"] for i in again["items"]].index(success)][
        "fields"]["kind"] == 6
    assert _eff(conn, ids["success"][2], "kind") == ("alert", "human")


def test_kind_asked_directly_on_set_3_is_scored_against_the_owners_own_kind_answers(conn, ingestor, keychain):
    _, sid, items, _, single = _set3(conn, ingestor)
    for p in items:
        gold.save_label(conn, sid, p, "kind", ["alert"] if p != single else ["fyi"])
    conn.commit()
    fake = FakeJev(pick=lambda body: {"kind": ("alert", 0.92)})
    res = focus.gold_run(conn, sid, "kind", client=jev_client(fake), progress=lambda s: None)
    assert len(fake.requests) == len(items) and all(list(r["questions"]) == ["kind"] for r in fake.requests)
    acc = res["accuracy"]["runs"][res["run_id"]]
    assert res["accuracy"]["items"] == len(items) and acc["decided"] == len(items)
    assert acc["right"] == len(items) - 1 and acc["mistakes"] == {"alert→fyi": 1}
    assert "Focused question kind on answer key" in focus.format_gold(res)
    # and the report takes it as Jev's direct kind
    rows = {r["labeller"]: r for r in gold.report(conn, sid)["labellers"]["rows"]}
    direct = next(r for n, r in rows.items() if n.startswith("jev (kind asked directly"))
    assert direct["fields"]["kind"]["right"] == len(items) - 1


def test_the_command_line_draws_set_3_archives_set_2_and_dry_runs_the_group_answers(conn, ingestor, database, vault):
    from test_gold_check import _cli
    ids = _unsure_archive(conn, ingestor)
    conn.execute("insert into gold_set (id, name, seed, target) values (2, 'Jev unsure, 15 items', 26, 1)")
    conn.execute("insert into gold_item (set_id, position, message_id, stratum, reason, unit)"
                 " values (2, 1, %s, 'machine', 'r', 'message')", (ids["news"][0],))
    conn.execute("select setval('gold_set_id_seq', 2)")
    conn.commit()
    out = _cli(["enrich", "gold", "archive", "--set", "2"], database, vault)
    assert "answer key 2 (Jev unsure, 15 items): archived" in out
    assert [s["id"] for s in gold.sets(conn)] == [] and gold.sets(conn, archived=True)[0]["archived"] is True
    out = _cli(["enrich", "gold", "sample-uncertain", "--n", "15", "--seed", "31", "--fields",
                "origin,kind,topic,ask,value,route"], database, vault)
    assert "answer key 3 (seed 31): 13 items labelling origin, kind, topic, ask, value, route" in out
    assert [s["id"] for s in gold.sets(conn)] == [3] and gold.sets(conn)[0]["fields"] == SET3
    out = _cli(["enrich", "gold", "propagate-groups", "--set", "3"], database, vault)
    assert out.startswith("DRY RUN (rolled back): answer key 3: 0 values written") and "no sure answer yet" in out
    with pytest.raises(SystemExit):
        _cli(["enrich", "gold", "sample-uncertain", "--seed", "31", "--fields", "origin,colour"], database, vault)
    _cli(["enrich", "gold", "archive", "--set", "2", "--undo"], database, vault)
    assert [s["id"] for s in gold.sets(conn)] == [3, 2]
