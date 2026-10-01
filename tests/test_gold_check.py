"""The answer key's labellers: Claude's labels imported, compared with the owner's, and the check screen
where the owner agrees with or corrects a frozen random sample of them. The blind screen stays blind.

Invented mail only (test_gold's archive); the taxonomy is loaded, as `talos setup` loads it.
"""

import json

import psycopg
import pytest
from psycopg import conninfo
from test_gold import ANSWER, POST, _full, _items
from test_web import client

from talos import cli, db, gold, personal

pytestmark = pytest.mark.usefixtures("taxonomy_loaded")

# One line per item, in the shape Claude writes.
LINE = {"origin": "marketing", "type": "promotion", "topic": "Shopping", "ask": [], "value": "noise", "route": [],
        "unsure": [], "note": None}


def _line(pos, **kw) -> str:
    return json.dumps({"position": pos, **LINE, **kw}, ensure_ascii=False)


def _write(tmp_path, name, lines) -> str:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _rows(conn, set_id, labeller):
    return {(r["position"], r["field"]): (r["values"], r["status"]) for r in conn.execute(
        "select i.position, l.field, l.values, l.status from gold_label l join gold_item i on i.id = l.item_id"
        " where l.set_id = %s and l.labeller = %s", (set_id, labeller))}


# ---------------------------------------------------------------- the migration

def test_the_migration_makes_every_existing_answer_the_owners_and_keys_answers_by_labeller(database):
    """Applied to a database with answers in it (the real one has two items labelled), every
    existing answer becomes the owner's (013's default, 'owner'; 032 drops it); afterwards two labellers can answer the same field."""
    dsn = conninfo.make_conninfo(database, dbname=conninfo.conninfo_to_dict(database)["dbname"] + "_mig")
    db.drop_database(dsn)
    db.ensure_database(dsn)
    try:
        with db.connect(dsn) as c:
            before = [(n, sql) for n, sql in db.migrations() if n < "013"]
            for _, sql in before:
                c.execute(sql)
            c.execute("insert into account (id, provider, address) values ('gmail', 'gmail', 'a@example.org')")
            mid = c.execute("insert into entity (kind) values ('message') returning id").fetchone()["id"]
            c.execute("insert into message (id, account_id, provider_key, direction, parser_version)"
                      " values (%s, 'gmail', 'k1', 'in', 1)", (mid,))
            c.execute("insert into gold_set (name, seed, target) values ('k', 1, 1)")
            c.execute("insert into gold_item (set_id, position, message_id, stratum, reason, unit)"
                      " values (1, 1, %s, 'person', 'r', 'message')", (mid,))
            c.execute("insert into gold_label (set_id, item_id, field, values) values (1, 1, 'origin', '{person}'),"
                      " (1, 1, 'note', '{svår}')")
            c.execute(dict(db.migrations())["013_gold_labeller.sql"])
            assert c.execute("select field, labeller from gold_label order by field").fetchall() == [
                {"field": "note", "labeller": "owner"}, {"field": "origin", "labeller": "owner"}]
            assert c.execute("select labeller from gold_label_valid where field = 'origin'").fetchone() == {
                "labeller": "owner"}
            c.execute("insert into gold_label (set_id, item_id, field, values, labeller)"
                      " values (1, 1, 'origin', '{marketing}', 'claude')")
            with pytest.raises(psycopg.errors.UniqueViolation):
                c.execute("insert into gold_label (set_id, item_id, field, values) values (1, 1, 'origin', '{list}')")
            c.rollback()
    finally:
        db.drop_database(dsn)


def test_a_label_is_the_owners_unless_another_labeller_is_named_and_a_labeller_is_never_empty(conn, ingestor):
    _full(conn, ingestor)
    sid = gold.sample(conn, n=6, seed=5)["set_id"]
    gold.save_label(conn, sid, 1, "origin", ["person"])
    gold.save_label(conn, sid, 1, "origin", ["marketing"], labeller="claude")
    assert _rows(conn, sid, personal.OWNER_ID) == {(1, "origin"): (["person"], "set")}
    assert _rows(conn, sid, "claude") == {(1, "origin"): (["marketing"], "set")}
    with pytest.raises(gold.GoldError):
        gold.save_label(conn, sid, 1, "origin", ["person"], labeller="")
    item1 = _items(conn, sid)[0]["id"]
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("insert into gold_label (set_id, item_id, field, values, labeller)"
                     " values (%s, %s, 'type', '{}', ' ')", (sid, item1))
    conn.rollback()


# ---------------------------------------------------------------- import

def test_an_import_saves_every_field_with_unsure_and_note_and_a_second_import_replaces_it(conn, ingestor, tmp_path,
                                                                                          database, vault):
    _full(conn, ingestor)
    sid = gold.sample(conn, n=8, seed=5)["set_id"]
    gold.save_label(conn, sid, 1, "origin", ["person"])  # the owner's: never touched by an import
    first = _write(tmp_path, "a.jsonl", [
        _line(1, route=["IT Operations", "Security"], ask=["question"]),
        "",
        _line(2, topic="Work/Backup", unsure=["topic", "ask"], note="Svårt att säga"),
        _line(3, origin=None, unsure=["origin"])])
    second = _write(tmp_path, "b.jsonl", [_line(4)])
    res = gold.import_labels(conn, sid, "claude", [first, second])
    assert (res["lines"], res["items"], res["new"], res["replaced"]) == (4, 4, 4, 0)
    assert (res["fields_set"], res["fields_unsure"], res["notes"]) == (21, 3, 1)
    assert res["unsure"] == {"topic": 1, "ask": 1, "origin": 1} and res["labelled_items"] == 4
    rows = _rows(conn, sid, "claude")
    assert rows[1, "route"] == (["IT Operations", "Security"], "set") and rows[1, "ask"] == (["question"], "set")
    assert rows[4, "ask"] == ([], "set") and rows[4, "route"] == ([], "set")  # an empty list is "none", sure
    assert rows[2, "topic"] == (["Work/Backup"], "unsure") and rows[2, "ask"] == ([], "unsure")
    assert rows[2, "note"] == (["Svårt att säga"], "set") and (1, "note") not in rows
    assert rows[3, "origin"] == ([], "unsure")
    assert len(rows) == 4 * 6 + 1
    assert _rows(conn, sid, personal.OWNER_ID) == {(1, "origin"): (["person"], "set")}
    # the same file again: the same labels, nothing added
    again = gold.import_labels(conn, sid, "claude", [first, second])
    assert (again["new"], again["replaced"]) == (0, 4) and _rows(conn, sid, "claude") == rows
    # a new answer for item 2 replaces all of Claude's answers for it (its note too); the others stay
    gold.import_labels(conn, sid, "claude", [_write(tmp_path, "c.jsonl", [_line(2, value="transient")])])
    rows2 = _rows(conn, sid, "claude")
    assert rows2[2, "value"] == (["transient"], "set") and rows2[2, "topic"] == (["Shopping"], "set")
    assert (2, "note") not in rows2 and rows2[1, "route"] == rows[1, "route"]
    # from the command line
    conn.commit()
    out = _cli(["enrich", "gold", "import-labels", "--set", str(sid), "--labeller", "claude", first], database, vault)
    assert "imported 3 items (3 lines) as claude" in out and "0 new, 3 replaced" in out
    assert "has now labelled 4 of 8 items" in out


def test_one_bad_line_refuses_the_whole_import_and_lists_every_problem(conn, ingestor, tmp_path):
    _full(conn, ingestor)
    sid = gold.sample(conn, n=8, seed=5)["set_id"]
    gold.import_labels(conn, sid, "claude", [_write(tmp_path, "ok.jsonl", [_line(5)])])
    before = _rows(conn, sid, "claude")
    path = _write(tmp_path, "bad.jsonl", [
        _line(1),                                   # good, but not written: the whole file is refused
        _line(2, origin="robot"),                   # not an origin
        _line(3, type=None),                        # no value and not marked unsure
        _line(4, route=["Brandväggen"], ask="question"),  # not a category; ask is a list
        _line(5, unsure=["colour"]),
        json.dumps({"position": 6, **{k: v for k, v in LINE.items() if k != "value"}}),  # value missing
        _line(99),                                  # no such item
        _line(1),                                   # item 1 twice
        "{not json",
        _line(7, asks=["question"]),                # a typo'd key
    ])
    with pytest.raises(gold.GoldError) as exc:
        gold.import_labels(conn, sid, "claude", [path])
    msg = str(exc.value)
    assert msg.startswith("refused, nothing was imported: 10 problems")
    for part in ("bad.jsonl:2: item 2: 'robot' is not an allowed origin", "bad.jsonl:3: item 3: choose a value for type",
                 "item 4: 'Brandväggen' is not an allowed route", "item 4: ask is a list of values",
                 "item 5: unsure is a list of fields", "item 6: value is missing", "position 99 is not an item",
                 "bad.jsonl:8: item 1 again (first at bad.jsonl:1)", "bad.jsonl:9: not JSON", "unknown key asks"):
        assert part in msg, part
    assert _rows(conn, sid, "claude") == before  # nothing written
    with pytest.raises(gold.GoldError, match="come from the screens"):
        gold.import_labels(conn, sid, personal.OWNER_ID, [_write(tmp_path, "me.jsonl", [_line(1)])])


# ---------------------------------------------------------------- blind

def test_the_blind_api_never_returns_claudes_labels_or_note(conn, ingestor, vault, database, tmp_path):
    _full(conn, ingestor)
    sid = gold.sample(conn, n=12, seed=2)["set_id"]
    # Claude answers every item, with values and a note no answer of the owner's could carry; so does Haiku.
    gold.import_labels(conn, sid, "claude", [_write(tmp_path, "c.jsonl", [
        _line(p, origin="alert", type="security_event", topic="Work/Backup", ask=["deadline", "my_commitment"],
              value="record_financial", route=["Security"], unsure=["topic"], note=f"CLAUDE-QUUX {p}")
        for p in range(1, 13)])])
    gold.import_labels(conn, sid, "haiku", [_write(tmp_path, "h.jsonl", [
        _line(p, origin="auto_reply", type="alarm", topic="Work/Backup", ask=["deadline"], value="memory",
              route=["Security"], note=f"HAIKU-ZORK {p}") for p in range(1, 13)])])
    _ = gold.save_label(conn, sid, 2, "origin", ["person"])  # one of the owner's, so labels are not simply empty
    gold.check_sample(conn, sid, n=5, seed=1)
    conn.commit()
    c = client(database, vault)
    # the list and the set carry the options (every value of every field), so only names are checked there
    for text in (c.get("/api/gold").text, c.get(f"/api/gold/{sid}").text):
        for word in ("QUUX", "ZORK", "claude", "haiku", "theirs", "labeller"):
            assert word not in text, (word, text[:300])
    texts = []
    for pos in range(1, 13):
        r = c.get(f"/api/gold/{sid}/items/{pos}")
        assert r.status_code == 200
        it = r.json()
        texts.append(r.text)
        assert it["labels"] == ({"origin": {"values": ["person"], "status": "set"}} if pos == 2 else {})
        assert all(r["done"] is False for r in it["round_items"])
    prog = c.get(f"/api/gold/{sid}").json()["progress"]
    assert prog["done"] == 0 and prog["next"] == 1 and prog["fields"]["origin"] == {"set": 1, "unsure": 0, "skip": 0}
    assert prog["fields"]["topic"] == {"set": 0, "unsure": 0, "skip": 0}
    assert c.get("/api/gold").json()["sets"][0]["done"] == 0
    # Claude's use of a value never orders the owner's Common group
    opts = c.get(f"/api/gold/{sid}").json()["options"]
    assert {o["value"]: o["used"] for o in opts["origin"]}["alert"] == 0
    for text in texts:
        for word in ("QUUX", "ZORK", "claude", "haiku", "security_event", "record_financial", "my_commitment", "alarm",
                     "auto_reply", "memory", "theirs", "labeller"):
            assert word not in text, (word, text[:300])
    # the check endpoints are the ones that carry them, and only there
    assert "QUUX" in c.get(f"/api/gold/{sid}/check/1").text


# ---------------------------------------------------------------- the check sample

def _claude_all(conn, sid, tmp_path, n, **kw):
    gold.import_labels(conn, sid, "claude", [_write(tmp_path, "all.jsonl", [_line(p, **kw) for p in range(1, n + 1)])])


def test_the_check_sample_is_frozen_deterministic_and_leaves_out_the_items_the_owner_labelled(conn, ingestor, tmp_path,
                                                                                      database, vault):
    _full(conn, ingestor)
    sid = gold.sample(conn, n=30, seed=3)["set_id"]
    _claude_all(conn, sid, tmp_path, 28)                        # items 29 and 30: no Claude labels
    for field, values in ANSWER.items():                        # item 1 labelled blind, whole
        gold.save_label(conn, sid, 1, field, values)
    gold.save_label(conn, sid, 2, "origin", ["person"])         # item 2, one field
    gold.save_label(conn, sid, 3, "note", ["bara en anteckning"])  # a note alone is not an answer
    a = gold.check_sample(conn, sid, n=10, seed=7)
    assert a["n"] == 10 and a["candidates"] == 26 and a["excluded"] == [1, 2]
    assert not {1, 2, 29, 30} & set(a["positions"]) and len(set(a["positions"])) == 10
    with pytest.raises(gold.GoldError, match="already has a check sample"):
        gold.check_sample(conn, sid, n=10, seed=7)
    b = gold.check_sample(conn, sid, n=10, seed=7, replace=True)
    assert b["positions"] == a["positions"]                     # the same seed, the same items, in the same order
    c = gold.check_sample(conn, sid, n=10, seed=8, replace=True)
    assert c["positions"] != a["positions"]
    st = gold.check_state(conn, sid)
    assert [i["position"] for i in st["items"]] == c["positions"] and st["seed"] == 8 and st["labeller"] == "claude"
    assert (st["total"], st["checked"], st["next"]) == (10, 0, 1)
    assert gold.check_sample(conn, sid, n=99, replace=True)["n"] == 26  # never more than there are; seed: the set's
    assert gold.check_state(conn, sid)["seed"] == 3
    # from the command line
    conn.commit()
    out = _cli(["enrich", "gold", "check-sample", "--set", str(sid), "--n", "5", "--seed", "7", "--replace"],
               database, vault)
    assert "5 of 26 items labelled by claude (seed 7)" in out and "you labelled them yourself: 1, 2" in out
    assert "items: " + ", ".join(map(str, gold.check_sample(conn, sid, n=5, seed=7, replace=True)["positions"])) in out


# ---------------------------------------------------------------- agree and correct

def test_agreeing_saves_claudes_value_as_the_owners_and_a_correction_saves_their_own(conn, ingestor, tmp_path, database, vault):
    _full(conn, ingestor)
    sid = gold.sample(conn, n=12, seed=5)["set_id"]
    _claude_all(conn, sid, tmp_path, 12, ask=["question"], route=["Security"], unsure=["topic"], note="Troligen reklam")
    sample = gold.check_sample(conn, sid, n=4, seed=1)["positions"]
    # the fourth: Claude was not sure of its origin and gave none
    gold.import_labels(conn, sid, "claude", [_write(tmp_path, "one.jsonl", [
        _line(sample[3], ask=["question"], route=["Security"], origin=None, unsure=["origin"])])])
    blind_pos = next(p for p in range(1, 13) if p not in sample)
    for field, values in ANSWER.items():   # an item the owner labelled blind: compared apart
        gold.save_label(conn, sid, blind_pos, field, values)
    conn.commit()
    st = gold.check_state(conn, sid)
    c = client(database, vault)
    assert c.get(f"/api/gold/{sid}").json()["check"] == {"total": 4, "checked": 0}
    one = c.get(f"/api/gold/{sid}/check/1").json()
    assert one["rank"] == 1 and one["check_total"] == 4 and one["position"] == st["items"][0]["position"]
    assert one["theirs"]["labeller"] == "claude" and one["theirs"]["note"] == "Troligen reklam"
    assert one["theirs"]["labels"]["topic"] == {"values": ["Shopping"], "status": "unsure"}
    assert one["labels"] == {} and one["message"]["id"]   # the item as the blind screen shows it, plus theirs
    blind = c.get(f"/api/gold/{sid}/items/{one['position']}").json()
    assert {k: v for k, v in one.items() if k not in ("rank", "check_total", "theirs")} == blind
    # item 1: the owner agrees with everything at once
    r = c.post(f"/api/gold/{sid}/check/1/agree", headers=POST, json={"fields": list(gold.FIELDS), "duration_ms": 9000})
    assert r.status_code == 200 and r.json()["check"]["checked"] == 1
    mine = _rows(conn, sid, personal.OWNER_ID)
    p1 = st["items"][0]["position"]
    assert mine[p1, "topic"] == (["Shopping"], "set")          # agreeing makes Claude's unsure value the owner's, sure
    assert mine[p1, "ask"] == (["question"], "set") and mine[p1, "route"] == (["Security"], "set")
    assert (p1, "note") not in mine                             # Claude's note stays Claude's
    # item 2: the owner agrees with two fields and corrects the rest through the ordinary label endpoint
    p2 = st["items"][1]["position"]
    assert c.post(f"/api/gold/{sid}/check/2/agree", headers=POST, json={"fields": ["origin", "value"]}).status_code == 200
    for field, values, status in (("type", ["newsletter"], "set"), ("topic", ["Work/Backup"], "set"),
                                  ("ask", [], "set"), ("route", ["Security", "Costs"], "unsure")):
        assert c.post(f"/api/gold/{sid}/items/{p2}/labels", headers=POST,
                      json={"field": field, "values": values, "status": status}).status_code == 200
    # item 4: Claude was not sure of origin and gave none, so there is nothing to agree with
    r = c.post(f"/api/gold/{sid}/check/4/agree", headers=POST, json={"fields": ["origin"]})
    assert r.status_code == 400 and "not sure of origin and gave no value" in r.json()["error"]
    assert c.post(f"/api/gold/{sid}/check/4/agree", headers=POST, json={"fields": ["colour"]}).status_code == 400
    assert c.post(f"/api/gold/{sid}/check/4/agree", json={"fields": ["type"]}).status_code == 403  # no X-Talos
    assert c.get(f"/api/gold/{sid}/check/9").status_code == 404
    # the summary: what the owner kept, what they changed, and the blind item apart
    s = c.get(f"/api/gold/{sid}/check/summary").json()
    assert (s["total"], s["checked"]) == (4, 2)
    assert s["fields"]["origin"] == {"answered": 2, "agreed": 2, "changed": [], "rate": 1.0}
    assert s["fields"]["type"]["agreed"] == 1 and s["fields"]["type"]["changed"] == [
        {"rank": 2, "position": p2, "from": {"values": ["promotion"], "status": "set"},
         "to": {"values": ["newsletter"], "status": "set"}}]
    assert s["fields"]["topic"]["changed"][0]["from"] == {"values": ["Shopping"], "status": "unsure"}
    assert s["fields"]["route"]["changed"][0]["to"] == {"values": ["Security", "Costs"], "status": "unsure"}
    assert s["overall"] == {"answered": 12, "agreed": 8, "rate": 0.6667}
    assert s["blind"]["items"] == 1 and s["blind"]["positions"] == [blind_pos]
    assert s["blind"]["overall"]["compared"] == 5            # Claude was not sure of its topic: not scored
    # the owner's checked answers are theirs: the blind screen's progress counts them
    assert gold.progress(conn, sid)["done"] == 3
    st2 = c.get(f"/api/gold/{sid}/check").json()["state"]
    assert (st2["checked"], st2["next"]) == (2, 3)


# ---------------------------------------------------------------- the agreement maths

def _g(values, status="set"):
    return {"values": values, "status": status}


def test_agreement_scores_sure_pairs_exactly_with_partial_credit_for_sets_and_counts_the_rest_apart():
    claude = {1: {"origin": _g(["person"]), "ask": _g(["question", "deadline"]), "topic": _g(["Shopping"])},
              2: {"origin": _g(["marketing"]), "ask": _g([]), "topic": _g(["Work/Backup"], "unsure")},
              3: {"origin": _g(["alert"]), "ask": _g(["question"]), "topic": _g([], "skip")},
              4: {"origin": _g(["person"]), "ask": _g([])},
              5: {"origin": _g(["person"])}}                     # the owner never answered item 5
    owner = {1: {"origin": _g(["person"]), "ask": _g(["question"]), "topic": _g([" shopping "])},
           2: {"origin": _g(["notification"]), "ask": _g([]), "topic": _g(["Work/Backup"])},
           3: {"origin": _g([], "unsure"), "ask": _g(["question", "action"], "unsure"), "topic": _g(["Shopping"])},
           4: {"origin": _g([], "skip"), "ask": _g(["action"])}}
    r = gold.agreement(claude, owner, fields=("origin", "ask", "topic"))
    assert r["items"] == 4
    o = r["fields"]["origin"]
    assert (o["items"], o["compared"], o["exact"], o["rate"]) == (4, 2, 1, 0.5)
    assert (o["a_unsure"], o["b_unsure"], o["a_skip"], o["b_skip"]) == (0, 1, 0, 1)
    assert (o["unsure_pairs"], o["unsure_match"]) == (1, 0)
    assert o["changes"] == {"marketing→notification": 1} and o["changed_items"] == [2]
    a = r["fields"]["ask"]
    # item 1: {question, deadline} vs {question}: not exact, overlap ½; item 2: none = none; item 4: none vs {action}
    assert (a["compared"], a["exact"], a["jaccard"]) == (3, 1, 0.5)
    assert a["unsure_pairs"] == 1 and a["unsure_match"] == 0
    assert a["changes"] == {"deadline,question→question": 1, "none→action": 1}
    t = r["fields"]["topic"]
    assert (t["compared"], t["exact"], t["a_unsure"], t["a_skip"], t["unsure_pairs"], t["unsure_match"]) == (1, 1, 1, 1, 1, 1)
    assert "jaccard" not in t
    assert r["overall"] == {"compared": 6, "exact": 3, "rate": 0.5}
    assert gold.jaccard("ask", [], []) == 1.0 and gold.jaccard("route", ["a", "b", "c"], ["a"]) == 0.3333


def test_the_report_can_take_claudes_labels_as_reference_and_shows_their_agreement_with_the_owners(
        conn, ingestor, tmp_path, database, vault):
    _full(conn, ingestor)
    sid = gold.sample(conn, n=10, seed=6)["set_id"]
    _claude_all(conn, sid, tmp_path, 10, origin="person", type="conversation", topic="Work/Customers",
                ask=["question"], value="transient", route=[])
    for pos in (1, 2):                                          # the owner labelled two blind; on item 2 they differ
        for field, values in ANSWER.items():
            gold.save_label(conn, sid, pos, field, ["notification"] if (pos, field) == (2, "origin") else values)
    gold.check_sample(conn, sid, n=3, seed=1)
    gold.agree(conn, sid, 1, list(gold.FIELDS))
    conn.commit()
    owners = gold.report(conn, sid)
    assert owners["labeller"] == personal.OWNER_ID and owners["done"] == 3
    theirs = gold.report(conn, sid, labeller="claude")
    assert theirs["labeller"] == "claude" and theirs["done"] == 10 and theirs["fields"]["origin"]["set"] == 10
    [ag] = owners["agreement"]
    assert ag["labeller"] == "claude" and ag["items"] == 10
    assert ag["all"]["items"] == 3 and ag["checked"]["items"] == 1 and ag["blind"]["items"] == 2
    assert ag["all"]["overall"] == {"compared": 18, "exact": 17, "rate": 0.9444}
    assert ag["all"]["fields"]["origin"]["changes"] == {"person→notification": 1}
    assert ag["all"]["fields"]["origin"]["changed_items"] == [2]
    text = gold.format_report(theirs)
    assert "10 of 10 items labelled by claude" in text and "against claude's labels" in text
    assert "Agreement, claude against your labels" in text and "items you labelled blind: 2 items" in text
    out = _cli(["enrich", "gold", "report", "--set", str(sid), "--labeller", "claude"], database, vault)
    assert "labelled by claude" in out and "person→notification 1 (items 2)" in out


def test_the_check_screen_is_reached_from_the_answer_key_and_deep_linkable():
    js = (__import__("pathlib").Path(__file__).parent.parent / "src" / "talos" / "web" / "static" / "app.js").read_text()
    assert "goldcheck: viewGoldCheck" in js and "'goldcheck'" in js
    assert "go('goldcheck')" in js and "'#goldcheck/' + rank" in js
    # the blind screen's code never reads the check endpoints
    blind = js[js.index("async function viewGold()"):js.index("// ---- the check: Claude's labels, checked")]
    assert "/check" not in blind and "theirs" not in blind


def _cli(argv, database, vault) -> str:
    import contextlib
    import io
    import os
    buf = io.StringIO()
    env = {"TALOS_DSN": database, "TALOS_HOME": str(vault.root.parent)}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    saved = cli._logging
    cli._logging = lambda settings, verbose: None
    try:
        with contextlib.redirect_stdout(buf):
            cli.main(argv)
    finally:
        cli._logging = saved
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return buf.getvalue()
