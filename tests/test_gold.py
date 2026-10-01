"""The answer key (enrichment step B): the frozen sample, blind labelling, the reveal and the scores.

Invented mail only (tests/mailfactory.py via test_enrich's archive). The taxonomy
(rules/taxonomy.json) is loaded for every test here, as `talos setup` loads it.
"""

import json
from datetime import timedelta

import mailfactory as mf
import pytest
from test_enrich import NOW, _archive, _keys, mail
from test_web import client

from talos import cli, enrich, gold, rules, taxonomy
from talos.ingest import Location

pytestmark = pytest.mark.usefixtures("taxonomy_loaded")

POST = {"X-Talos": "1"}
ANSWER = {"origin": ["person"], "type": ["conversation"], "topic": ["Work/Customers"], "ask": ["question"],
          "value": ["transient"], "route": []}
FILE = {dim: d["values"] for dim, d in taxonomy.read().items()}


def _teams(conn, ingestor, chat: str, minutes: list[int], *, chat_type: str = "group", mine=()) -> list[int]:
    """A Teams chat: one message per minute offset (a gap of 2 h or more starts a new window)."""
    t0 = NOW - timedelta(days=2)
    ids = []
    for i, m in enumerate(minutes):
        n = next(_keys)
        frm = mf.ME if i in mine else "Pia Ek <pia.ek@company.example>"
        ids.append(ingestor.ingest("gmail", mf.make(frm=frm, subject=chat, body=f"rad {i}", msgid=f"<t{n}@test.invalid>"),
                                   Location("[all]", f"t{n}", provider_thread_id=chat,
                                            received_at=t0 + timedelta(minutes=m))).message_id)
    conn.execute("update message set medium = 'teams_chat', headers = headers || jsonb_build_object("
                 "'x-teams-chat-type', %s::text) where id = any(%s)", (chat_type, ids))
    return ids


def _full(conn, ingestor) -> dict:
    """test_enrich's archive (one message per origin signal), plus a big template, old and
    recent person threads, legacy mail, a bulk unknown sender and three Teams chats."""
    ids = _archive(conn, ingestor)
    ids["veeam"] = [mail(ingestor, frm="Veeam <veeam@backup.nordvik.se>", subject=f"[Success] Backup job NAS0{i}",
                         headers={"Auto-Submitted": "auto-generated"}, days_ago=30 + i) for i in range(6)]
    ids["old"] = [mail(ingestor, frm=f"Vän {i} <van{i}@example.org>", subject=f"Minns du {i}?", thread=f"old{i}",
                       days_ago=400 + i) for i in range(3)]
    ids["recent"] = [mail(ingestor, frm=f"Kollega {i} <kollega{i}@company.example>", subject=f"Fråga {i}",
                          thread=f"new{i}", days_ago=3 + i) for i in range(3)]
    ids["year"] = mail(ingestor, frm="Revisor <revisor@example.org>", subject="Bokslut", thread="y1", days_ago=200)
    rules.save(conn, rules.Rule("rt", "Klarna is shopping", [{"field": "from_domain", "op": "is", "value": "klarna.com"}],
                                {"dimension": "topic", "value": "Shopping"}))
    rules.run_all(conn)  # a receipt with a topic: a boundary case
    legacy = mail(ingestor, frm="Blocket <annons@blocket.se>", subject="Din annons är publicerad")
    rules.assign(conn, [legacy], "tag", "legacy:get-rid-of")
    ids["legacy"] = legacy
    ids["bulk"] = [mail(ingestor, frm="Föreningen <info@forening.example>", subject=f"Medlemsbrev {i}",
                        days_ago=10 + i) for i in range(21)]
    ids["chat_a"] = _teams(conn, ingestor, "chatA", [0, 10, 20, 300, 305], mine={1})
    ids["chat_b"] = _teams(conn, ingestor, "chatB", [0, 5], chat_type="oneOnOne")
    ids["chat_c"] = _teams(conn, ingestor, "chatC", [0, 1, 2], chat_type="meeting")
    enrich.prepass(conn)
    conn.commit()
    return ids


def _items(conn, set_id):
    return conn.execute("select * from gold_item where set_id = %s order by position", (set_id,)).fetchall()


# ---------------------------------------------------------------- the sample

def test_strata_are_split_in_proportion_and_always_sum_to_n():
    assert gold._split(300, [50, 100, 100, 50]) == [50, 100, 100, 50]
    assert sum(gold._split(31, [50, 100, 100, 50])) == 31
    assert gold._split(50, [15, 8, 7, 7, 6, 7]) == [15, 8, 7, 7, 6, 7]
    assert sum(gold._split(10, [15, 8, 7, 7, 6, 7])) == 10


def test_the_sample_draws_every_stratum_and_anchors_each_item_with_its_context(conn, ingestor):
    ids = _full(conn, ingestor)
    res = gold.sample(conn, n=30, seed=3)
    items = _items(conn, res["set_id"])
    assert len(items) == res["items"] == 30
    assert len({i["message_id"] for i in items}) == 30  # no message twice
    assert {i["stratum"] for i in items} == {"machine", "person", "teams", "boundary"}
    assert all(i["reason"].startswith(i["stratum"] + " · ") for i in items)
    groups = {i["info"]["group"] for i in items}
    assert {"bulk_mailer", "correspondent", "big", "tail", "recent", "older", "group"} <= groups
    # no thread or pattern is drawn twice (a Teams window claims only its window)
    threads = [i["thread_id"] for i in items if i["thread_id"] and i["unit"] != "window"]
    assert len(threads) == len(set(threads))
    patterns = [i["pattern_key"] for i in items if i["pattern_key"]]
    assert len(patterns) == len(set(patterns))
    for i in items:
        m = conn.execute("select thread_id, medium from message where id = %s", (i["message_id"],)).fetchone()
        if i["unit"] == "window":  # a window: first and last message in the anchor's chat, the anchor between
            assert m["medium"] == "teams_chat" and i["stratum"] == "teams"
            span = conn.execute("select array_agg(id order by received_at, id) as ids from message where thread_id = %s",
                                (m["thread_id"],)).fetchone()["ids"]
            lo, hi = span.index(i["window_first_id"]), span.index(i["window_last_id"])
            assert lo <= span.index(i["message_id"]) <= hi
        if i["unit"] == "pattern":  # the pattern's other samples, frozen
            key = conn.execute("select pattern_key from message_pattern where message_id = %s",
                               (i["message_id"],)).fetchone()["pattern_key"]
            assert key == i["pattern_key"] and i["message_id"] not in i["context_ids"]
            assert all(conn.execute("select pattern_key from message_pattern where message_id = %s", (c,)).fetchone()
                       ["pattern_key"] == key for c in i["context_ids"])
    # the chat with a four-hour gap gives two windows; the anchor's is the one stored
    a = [i for i in items if i["message_id"] in ids["chat_a"]]
    assert all((i["window_first_id"], i["window_last_id"]) in ((ids["chat_a"][0], ids["chat_a"][2]),
                                                              (ids["chat_a"][3], ids["chat_a"][4])) for i in a)
    # the big template was drawn from the machine stratum, anchored on one of its messages
    big = [i for i in items if i["info"]["group"] == "big"]
    assert big and all(i["unit"] == "pattern" and i["info"]["pattern_messages"] >= 3 for i in big)
    # the labelling order is shuffled: positions 1..n
    assert [i["position"] for i in items] == list(range(1, 31))


def test_the_same_seed_draws_the_same_items_and_a_new_sample_never_changes_an_old_set(conn, ingestor):
    _full(conn, ingestor)
    first = gold.sample(conn, n=30, seed=11)
    shape = lambda sid: [(i["position"], i["message_id"], i["stratum"], i["reason"], i["unit"], i["context_ids"],
                          i["window_first_id"], i["window_last_id"]) for i in _items(conn, sid)]
    before = shape(first["set_id"])
    gold.save_label(conn, first["set_id"], 1, "origin", ["person"])
    again = gold.sample(conn, n=30, seed=11)
    assert again["set_id"] != first["set_id"] and shape(again["set_id"]) == before  # deterministic
    other = gold.sample(conn, n=30)  # no seed: one more than the highest used
    assert other["seed"] == 12 and shape(other["set_id"]) != before
    assert shape(first["set_id"]) == before  # the old set is frozen
    assert gold.progress(conn, first["set_id"])["fields"]["origin"]["set"] == 1
    dry = gold.sample(conn, n=30, seed=11, dry_run=True)
    assert "set_id" not in dry and dry["strata"] == first["strata"]
    assert conn.execute("select count(*) as n from gold_set").fetchone()["n"] == 3


def test_a_small_archive_hands_a_short_groups_share_to_the_others(conn, ingestor):
    _full(conn, ingestor)
    res = gold.sample(conn, n=300, seed=1)
    pools = sum(g["pool"] for g in res["groups"].values())
    assert 0 < res["items"] <= pools and res["items"] < 300
    assert sum(res["strata"].values()) == res["items"]
    assert res["strata"]["teams"] == 4  # every window there is (chat A has two)
    assert all(res["groups"][g]["items"] >= 1 for g in ("legacy_get_rid_of", "bulk_unknown", "platform_person",
                                                       "receipt_topic", "year", "oneOnOne", "meeting"))


# ---------------------------------------------------------------- labelling

def _label_all(c, set_id, pos, answer=ANSWER, duration=4000):
    for field, values in answer.items():
        r = c.post(f"/api/gold/{set_id}/items/{pos}/labels", headers=POST,
                   json={"field": field, "values": values, "duration_ms": duration})
        assert r.status_code == 200, r.text
    return r.json()


def test_labels_save_as_they_are_given_resume_where_left_and_can_be_changed(conn, ingestor, vault, database):
    _full(conn, ingestor)
    sid = gold.sample(conn, n=12, seed=5)["set_id"]
    conn.commit()
    c = client(database, vault)
    assert c.get("/api/gold").json()["sets"][0]["id"] == sid
    meta = c.get(f"/api/gold/{sid}").json()
    assert meta["progress"]["next"] == 1 and meta["progress"]["done"] == 0 and meta["progress"]["rounds"] == 2
    assert [o["value"] for o in meta["options"]["origin"]] == list(enrich.ORIGINS)
    assert "receipt" in [o["value"] for o in meta["options"]["type"]]
    res = _label_all(c, sid, 1)
    assert res["progress"]["done"] == 1 and res["progress"]["next"] == 2
    assert res["progress"]["session"]["number"] == 1 and res["progress"]["session"]["active"]
    # half of item 2, then the screen is closed: it resumes at item 2 with what was given
    c.post(f"/api/gold/{sid}/items/2/labels", headers=POST, json={"field": "origin", "values": [], "status": "unsure"})
    c.post(f"/api/gold/{sid}/items/2/labels", headers=POST, json={"field": "note", "values": ["svår"]})
    meta = c.get(f"/api/gold/{sid}").json()
    assert meta["progress"]["next"] == 2 and meta["progress"]["fields"]["origin"] == {"set": 1, "unsure": 1, "skip": 0}
    item2 = c.get(f"/api/gold/{sid}/items/2").json()
    assert item2["labels"] == {"origin": {"values": [], "status": "unsure"}, "note": {"values": ["svår"], "status": "set"}}
    # back to item 1: a changed answer replaces the old one
    c.post(f"/api/gold/{sid}/items/1/labels", headers=POST, json={"field": "origin", "values": ["notification"]})
    rows = conn.execute("select field, values from gold_label l join gold_item i on i.id = l.item_id"
                        " where i.set_id = %s and i.position = 1 and field = 'origin'", (sid,)).fetchall()
    assert rows == [{"field": "origin", "values": ["notification"]}]
    assert c.get(f"/api/gold/{sid}/items/1").json()["labels"]["origin"]["values"] == ["notification"]
    # an empty note is removed
    c.post(f"/api/gold/{sid}/items/2/labels", headers=POST, json={"field": "note", "values": []})
    assert "note" not in c.get(f"/api/gold/{sid}/items/2").json()["labels"]
    # nothing here wrote an assignment
    assert not conn.execute("select 1 from assignment where source_kind = 'human' and dimension_id <> 'tag'").fetchone()


def test_every_field_offers_its_dimensions_values_in_file_order_with_family_label_and_description(conn):
    opts = gold.options(conn)
    for field in gold.FIELDS:
        assert opts[field] == [{**{k: v[k] for k in ("value", "family", "label", "description")},
                                "common": int(v.get("common") or 0), "used": 0} for v in FILE[field]], field
    assert opts["topic_closed"] is True
    assert [o["value"] for o in opts["route"]][:3] == ["IT Operations", "Security", "Costs"]  # categories, by name


def test_a_label_must_be_one_of_the_fields_allowed_values(conn, ingestor, vault, database):
    ids = _full(conn, ingestor)
    sid = gold.sample(conn, n=6, seed=5)["set_id"]
    project = conn.execute("insert into entity (kind) values ('object') returning id").fetchone()["id"]
    conn.execute("insert into object (id, kind, name) values (%s, 'project', 'Brandväggen')", (project,))
    conn.commit()
    c = client(database, vault)
    url = f"/api/gold/{sid}/items/1/labels"
    bad = [{"field": "origin", "values": ["robot"]}, {"field": "origin", "values": ["person", "list"]},
           {"field": "type", "values": []}, {"field": "type", "values": ["personal"]},
           {"field": "ask", "values": ["gossip"]}, {"field": "value", "values": ["gold"]},
           {"field": "value", "values": ["record"]},  # the old list's value, split in the taxonomy
           {"field": "route", "values": [str(project)]},  # route is a category now, not an object id
           {"field": "route", "values": ["Brandväggen"]}, {"field": "colour", "values": ["red"]},
           {"field": "origin", "values": ["person"], "status": "maybe"}, {"field": "topic", "values": ["x" * 200]},
           {"field": "topic", "values": ["Något helt nytt"]}]  # topic is a closed list now
    for body in bad:
        assert c.post(url, headers=POST, json=body).status_code == 400, body
    ok = [{"field": "route", "values": ["IT Operations", "Security"]},
          {"field": "ask", "values": ["question", "my_commitment", "deadline"]},
          {"field": "topic", "values": ["Work/Backup"]}, {"field": "type", "values": [], "status": "skip"},
          {"field": "origin", "values": ["mail_system"]}, {"field": "value", "values": ["record_financial"]}]
    for body in ok:
        assert c.post(url, headers=POST, json=body).status_code == 200, body
    assert c.get(f"/api/gold/{sid}/items/1").json()["labels"]["route"]["values"] == ["IT Operations", "Security"]
    assert c.post(url, json={"field": "origin", "values": ["person"]}).status_code == 403  # no X-Talos
    assert c.get(f"/api/gold/{sid}/items/99").status_code == 404
    assert ids


# ---------------------------------------------------------------- blind

# The only keys a blind message may carry: who, when, what it says, and its files.
BLIND_KEYS = {"id", "account_id", "medium", "received_at", "direction", "from_name", "from_address", "subject",
              "has_attachments", "chat_type", "edited", "deleted", "teams_importance", "body_text", "quote_stripped",
              "body_kind", "participants", "attachments", "text"}


def test_the_blind_api_never_returns_a_rule_model_or_prepass_value(conn, ingestor, vault, database):
    _full(conn, ingestor)
    # Mark every message with values no label could contain: a rule topic, a model proposal and a
    # model value accepted on its thread, and an importance score.
    rules.save(conn, rules.Rule("xyz", "Everything is XYZZY", [{"field": "medium", "op": "is", "value": "email"}],
                                {"dimension": "tag", "value": "Tag-XYZZY"}))
    rules.save(conn, rules.Rule("xyt", "Teams too", [{"field": "medium", "op": "is", "value": "teams_chat"}],
                                {"dimension": "topic", "value": "Work/Company"}))
    rules.run_all(conn)
    conn.execute("insert into model_run (id, model, purpose) values ('jev-run-PLUGH', 'jev', 'test')")
    conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, status, confidence)"
                 " select id, 'type', 'invoice', 'model', 'jev-run-PLUGH', 'proposed', 0.91 from message")
    conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, status, confidence)"
                 " select id, 'tag', 'Model-PLUGH', 'model', 'jev-run-PLUGH', 'active', 0.93 from thread")
    conn.execute("insert into message_importance (message_id, score, level, version)"
                 " select id, 77, 'high', 1 from message")
    conn.execute("update message_location set labels = array['Label-FROB']")
    sid = gold.sample(conn, n=30, seed=2)["set_id"]
    conn.commit()
    c = client(database, vault)
    forbidden = ["XYZZY", "PLUGH", "FROB", "prepass", "rule:", '"source_kind"', '"source_ref"', '"confidence"',
                 '"is_automated"', '"importance"', '"stratum"', '"reason"', '"info"', '"evidence"', '"values": [{',
                 "labels\": [",
                 *(i["reason"] for i in _items(conn, sid))]
    everything = [c.get("/api/gold").text, c.get(f"/api/gold/{sid}").text]
    for pos in range(1, 31):
        r = c.get(f"/api/gold/{sid}/items/{pos}")
        assert r.status_code == 200
        it = r.json()
        everything.append(r.text)
        for m in [it["message"], *it["context"], *it["before"]]:
            assert set(m) <= BLIND_KEYS, set(m) - BLIND_KEYS
        assert it["labels"] == {}
    # the options are the closed lists, the same for every item
    options = json.loads(everything[1])["options"]
    assert [o["value"] for o in options["topic"]] == [v["value"] for v in FILE["topic"]]
    for text in everything:
        for word in forbidden:
            assert word not in text, (word, text[:300])
    # and the reveal refuses until the round is done
    assert c.post(f"/api/gold/{sid}/rounds/1/reveal", headers=POST).status_code == 409


def test_a_round_is_revealed_only_when_done_and_a_later_change_is_marked(conn, ingestor, vault, database):
    _full(conn, ingestor)
    sid = gold.sample(conn, n=8, seed=4)["set_id"]
    conn.commit()
    c = client(database, vault)
    for pos in range(1, 6):
        _label_all(c, sid, pos)
    assert c.post(f"/api/gold/{sid}/rounds/1/reveal", headers=POST).status_code == 409
    assert c.post(f"/api/gold/{sid}/rounds/1/reveal").status_code == 403
    _label_all(c, sid, 6)
    r = c.post(f"/api/gold/{sid}/rounds/1/reveal", headers=POST)
    assert r.status_code == 200
    rev = r.json()
    assert [x["position"] for x in rev["items"]] == [1, 2, 3, 4, 5, 6] and rev["seconds"] == 24
    assert all(x["stratum"] and x["reason"] for x in rev["items"])
    origins = [x["rules"].get("origin") for x in rev["items"]]
    compared = [o for o in origins if o]
    assert compared and all(o["source"].startswith(("prepass ", "rule ")) for o in compared)
    assert rev["agreement"]["origin"]["compared"] == len(compared)
    assert rev["agreement"]["origin"]["agree"] == sum(o["value"] == "person" for o in compared)
    assert c.get(f"/api/gold/{sid}/items/1").json()["revealed"] is True
    c.post(f"/api/gold/{sid}/items/1/labels", headers=POST, json={"field": "value", "values": ["noise"]})
    marked = conn.execute("select field from gold_label l join gold_item i on i.id = l.item_id where i.set_id = %s"
                          " and l.after_reveal", (sid,)).fetchall()
    assert marked == [{"field": "value"}]
    assert gold.report(conn, sid)["after_reveal"] == 1


def test_an_answer_whose_value_left_its_list_reads_as_unset_and_the_field_opens_again(conn, ingestor, vault,
                                                                                         database):
    _full(conn, ingestor)
    sid = gold.sample(conn, n=6, seed=5)["set_id"]
    for field, values in ANSWER.items():
        gold.save_label(conn, sid, 1, field, values)
    item1 = conn.execute("select id from gold_item where set_id = %s and position = 1", (sid,)).fetchone()["id"]
    # labels saved before the taxonomy: a route as an object id, the old value "record"
    conn.execute("update gold_label set values = '{4711}' where item_id = %s and field = 'route'", (item1,))
    conn.execute("update gold_label set values = '{record}' where item_id = %s and field = 'value'", (item1,))
    conn.commit()
    c = client(database, vault)
    it = c.get(f"/api/gold/{sid}/items/1").json()
    assert set(it["labels"]) == {"origin", "type", "topic", "ask"}  # route and value are open again
    assert [r["done"] for r in it["round_items"]][0] is False
    prog = c.get(f"/api/gold/{sid}").json()["progress"]
    assert prog["done"] == 0 and prog["next"] == 1 and prog["fields"]["route"] == {"set": 0, "unsure": 0, "skip": 0}
    assert gold.gold_labels(conn, sid)[item1].keys() == {"origin", "type", "topic", "ask"}
    assert c.get("/api/gold").json()["sets"][0]["done"] == 0
    # answering again replaces them
    for field in ("route", "value"):
        assert c.post(f"/api/gold/{sid}/items/1/labels", headers=POST,
                      json={"field": field, "values": ANSWER[field]}).status_code == 200
    assert c.get(f"/api/gold/{sid}").json()["progress"]["done"] == 1
    assert conn.execute("select values from gold_label where item_id = %s and field = 'route'",
                        (item1,)).fetchone()["values"] == []


# ---------------------------------------------------------------- evaluation

def _g(values, status="set"):
    return {"values": values, "status": status}


def test_compare_counts_precision_per_value_source_and_confidence_bucket():
    P = gold.Prediction
    labels = {1: {"origin": _g(["person"])}, 2: {"origin": _g(["person"])}, 3: {"origin": _g(["marketing"])},
              4: {"origin": _g(["transactional"])}, 5: {"origin": _g([], "unsure")}, 6: {"origin": _g([], "skip")},
              7: {"origin": _g(["person"])}}
    preds = {1: {"origin": P(("person",), 0.97, "prepass thread_reply")},
             2: {"origin": P(("marketing",), 0.6, "prepass bulk_mailer")},
             3: {"origin": P(("marketing",), 0.9, "prepass bulk_mailer")},
             4: {"origin": P(("marketing",), 0.9, "prepass bulk_mailer")},
             5: {"origin": P(("person",), 0.99, "prepass thread_reply")}}  # the owner was not sure: not scored
    r = gold.compare(labels, preds, fields=("origin",))["origin"]
    assert (r["labelled"], r["unsure"], r["skipped"], r["predicted"], r["correct"]) == (5, 1, 1, 4, 2)
    assert r["coverage"] == 0.8 and r["accuracy"] == 0.5
    assert r["per_value"]["marketing"] == {"predicted": 3, "correct": 1, "precision": 0.3333, "gold": 1, "recall": 1.0}
    assert r["per_value"]["person"] == {"predicted": 1, "correct": 1, "precision": 1.0, "gold": 3, "recall": 0.3333}
    assert r["per_value"]["transactional"]["recall"] == 0.0
    bm = r["by_source"]["prepass bulk_mailer"]
    assert (bm["n"], bm["correct"], bm["precision"]) == (3, 1, 0.3333)
    assert bm["wrong"] == {"marketing→person": 1, "marketing→transactional": 1} and bm["wrong_items"] == [2, 4]
    assert r["by_bucket"] == {"0.50–0.70": {"n": 1, "correct": 0, "precision": 0.0},
                              "0.85–0.95": {"n": 2, "correct": 1, "precision": 0.5},
                              "0.95–1.00": {"n": 1, "correct": 1, "precision": 1.0}}
    assert gold.bucket(1.0) == "0.95–1.00" and gold.bucket(0.2) == "0.00–0.50" and gold.bucket(None) == "none"


def test_compare_scores_many_value_fields_by_value_and_as_yes_or_no():
    P = gold.Prediction
    labels = {1: {"ask": _g(["question"])}, 2: {"ask": _g([])}, 3: {"ask": _g(["action", "decision"])},
              4: {"ask": _g([])}, 5: {"topic": _g(["Försäkring"])}}
    preds = {1: {"ask": P(("question",), 0.9)}, 2: {"ask": P(("question",), 0.8)}, 3: {"ask": P(("action",), 0.8)},
             4: {"ask": P((), 0.8)}, 5: {"topic": P((" försäkring ",), 0.8)}}
    r = gold.compare(labels, preds, fields=("ask", "topic"))
    a = r["ask"]
    assert a["per_value"]["question"] == {"tp": 1, "fp": 1, "fn": 0, "precision": 0.5, "recall": 1.0}
    assert a["per_value"]["decision"] == {"tp": 0, "fp": 0, "fn": 1, "precision": None, "recall": 0.0}
    assert a["any"] == {"tp": 2, "fp": 1, "fn": 0, "tn": 1, "precision": 0.6667, "recall": 1.0}
    assert a["exact"] == 0.5 and a["correct"] == 2
    assert r["topic"]["correct"] == 1  # topics compare without case or spaces


def test_the_report_scores_the_rules_and_a_model_run_against_the_owners_labels(conn, ingestor, vault, database, monkeypatch,
                                                                       capsys):
    _full(conn, ingestor)
    sid = gold.sample(conn, n=10, seed=6)["set_id"]
    items = _items(conn, sid)
    for i in items:  # the owner says every item is from a person, a conversation
        for field, values in ANSWER.items():
            gold.save_label(conn, sid, i["position"], field, values, duration_ms=1000)
    conn.execute("insert into model_run (id, model, purpose) values ('jev-1', 'jev', 'gold')")
    for n, i in enumerate(items):
        conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, status,"
                     " confidence) values (%s, 'origin', %s, 'model', 'jev-1', 'proposed', %s)",
                     (i["message_id"], "person" if n % 2 == 0 else "marketing", 0.9 if n % 2 == 0 else 0.6))
    conn.commit()
    rep = gold.report(conn, sid, run_id="jev-1")
    assert rep["done"] == rep["total"] == 10 and rep["fields"]["origin"]["set"] == 10
    ro = rep["rules"]["origin"]
    expected = conn.execute(
        "select count(*) filter (where e.value = 'person') as right, count(*) as n from gold_item i"
        " join effective_message_assignment e on e.message_id = i.message_id and e.dimension_id = 'origin'"
        " where i.set_id = %s", (sid,)).fetchone()
    assert (ro["correct"], ro["predicted"]) == (expected["right"], expected["n"])
    jev = rep["run"]["scores"]["origin"]
    assert (jev["predicted"], jev["correct"]) == (10, 5)
    assert jev["by_bucket"]["0.85–0.95"] == {"n": 5, "correct": 5, "precision": 1.0}
    assert jev["by_source"]["jev-1"]["wrong_items"] == sorted(i["position"] for n, i in enumerate(items) if n % 2)
    text = gold.format_report(rep)
    assert "Labels per field" in text and "Rules and the pre-pass" in text and "Model run jev-1" in text
    # from the command line
    monkeypatch.setenv("TALOS_DSN", database)
    monkeypatch.setenv("TALOS_HOME", str(vault.root.parent))
    monkeypatch.setattr(cli, "_logging", lambda settings, verbose: None)
    cli.main(["enrich", "gold", "report", "--set", str(sid)])
    out = capsys.readouterr().out
    assert "10 of 10 items done" in out and "gold-report-" in out
    cli.main(["enrich", "gold", "sample", "--n", "6", "--seed", "9", "--dry-run"])
    assert "nothing was stored" in capsys.readouterr().out
    cli.main(["enrich", "gold", "sample", "--n", "6", "--seed", "9"])
    assert "answer key" in capsys.readouterr().out
    cli.main(["enrich", "gold", "list"])
    assert "seed 9" in capsys.readouterr().out


def test_sessions_start_after_half_an_hour_without_an_answer():
    from datetime import datetime, timezone
    t = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)
    times = [t, t + timedelta(minutes=5), t + timedelta(minutes=50), t + timedelta(minutes=52)]
    s = gold._sessions(times, t + timedelta(minutes=60))
    assert (s["count"], s["active"], s["number"], s["started_at"]) == (2, True, 2, t + timedelta(minutes=50))
    later = gold._sessions(times, t + timedelta(hours=5))
    assert (later["active"], later["number"]) == (False, 3)
    assert gold._sessions([], t)["number"] == 1


def test_the_answer_key_view_is_in_the_rail_and_keeps_rule_values_out_of_the_item(database, vault):
    js = (__import__("pathlib").Path(__file__).parent.parent / "src" / "talos" / "web" / "static" / "app.js").read_text()
    assert "['gold', 'Answer key']" in js and "gold: viewGold" in js
    # the labelling screen reads only the blind endpoints; the message pane (with values) is never opened from it
    body = js[js.index("// ---------------------------------------------------------------- the answer key"):
              js.index("const RENDER = {")]
    assert "/api/messages" not in body and "openMessage(" not in body and "openThread(" not in body
    assert "{blind: true}" in body


def test_the_common_group_is_ordered_by_the_owners_own_use_and_never_by_the_item(conn, ingestor, vault, database):
    _full(conn, ingestor)
    sid = gold.sample(conn, n=6, seed=5)["set_id"]
    conn.commit()
    c = client(database, vault)
    first = c.get(f"/api/gold/{sid}/items/1").json()
    c.post(f"/api/gold/{sid}/items/1/labels", headers={"x-talos": "1"}, json={"field": "type", "values": ["spam"]})
    opts = gold.options(conn)
    used = {o["value"]: o["used"] for o in opts["type"]}
    assert used["spam"] == 1 and sum(used.values()) == 1
    assert any(o["common"] for o in opts["type"]) and "used" not in first  # the item itself carries no counts
