"""The enrichment backfill (talos.backfill): units per stage, records the answer key would build,
runs (dry run, cost guard, budget, resume), proposals and their propagation, accept and unaccept.

Never the real Jev: every request goes to test_jev's fake TypeSafe endpoint (httpx.MockTransport).
Invented mail only.
"""

import json

import pytest
from test_enrich import ME, mail
from test_gold import _full, _items, _teams
from test_jev import FakeJev, _no_sleep, keychain  # noqa: F401  (keychain is a fixture)

from talos import backfill, cli, db, enrich, gold, jev, rules, search

pytestmark = pytest.mark.usefixtures("taxonomy_loaded")

TELIA = "Telia <faktura@telia.se>"
PIA = "Pia Ek <pia.ek@nordvik.se>"


def client(fake: FakeJev, **kw) -> jev.JevClient:
    kw.setdefault("sleep", _no_sleep)
    return jev.JevClient(transport=fake.transport, **kw)


def _archive(conn, ingestor) -> dict:
    """A small archive with one unit of each kind:
    machine: a template of five invoices (three samples), a tail template of two codes and one of one;
    person: a thread with Pia (three of hers, one of the owner's), and a single letter;
    Teams: a chat of two windows (a gap of 5 h), and one of 35 lines a minute apart (split at 30)."""
    ids = {}
    auto = {"Auto-Submitted": "auto-generated"}
    ids["invoices"] = [mail(ingestor, frm=TELIA, subject=f"Faktura {2026_01 + i} från Telia", body=f"Faktura nr {i}",
                            headers=auto, days_ago=100 - 10 * i) for i in range(5)]
    ids["codes"] = [mail(ingestor, frm="Banken <noreply@banken.se>", subject=f"Din kod {1000 + i}", body=f"Kod {i}",
                         headers=auto, days_ago=20 - i) for i in range(2)]
    ids["welcome"] = mail(ingestor, frm="Butiken <noreply@butiken.se>", subject="Välkommen", body="Välkommen!",
                          headers=auto, days_ago=400)
    ids["pia"] = [mail(ingestor, frm=PIA, subject="Offert", body=f"Pia skriver {i}", thread="pia", days_ago=9 - i)
                  for i in range(3)]
    ids["owners"] = mail(ingestor, frm=ME, to=PIA, subject="Re: Offert", body="Tack, jag kollar.", thread="pia", days_ago=5)
    ids["pia"].append(mail(ingestor, frm=PIA, subject="Re: Offert", body="Pia skriver 3", thread="pia", days_ago=4))
    ids["letter"] = mail(ingestor, frm="Anna Berg <anna@example.org>", subject="Hej", body="Ses vi?", thread="anna",
                         days_ago=700)
    ids["chat"] = _teams(conn, ingestor, "chatA", [0, 10, 20, 300, 305], mine={1})
    ids["long"] = _teams(conn, ingestor, "chatL", list(range(35)))
    enrich.prepass(conn)
    conn.commit()
    return ids


def _pick(body: dict) -> dict:
    """Jev's answers by the anchor's text: invoices agree on origin, type and topic and split on
    value (nr 2 is a record); Pia's thread asks a question; the rest take each first value."""
    text = body["state"].get("text") or ""
    if "Faktura nr" in text:
        return {"origin": ("notification", 0.95), "type": ("invoice", 0.93), "topic": ("Finance/Payments", 0.9),
                "value": ("record_financial", 0.9) if "nr 2" in text else ("transient", 0.9)}
    if "Pia skriver" in text:
        return {"origin": ("person", 0.97), "type": ("request", 0.8), "topic": ("Work/IT operations", 0.9),
                "ask": ("question", 0.88), "route": ("Customers & Business", 0.86), "value": ("context", 0.6)}
    if "Ses vi?" in text:  # a route for personal mail: the topic gate forces it to none
        return {"route": ("Security", 0.9)}
    return {}


def _rows(conn, run_id, field=None) -> dict[tuple[int, str], dict]:
    q = ("select entity_id, dimension_id, value, status, confidence, evidence, decided_by from assignment"
         " where source_kind = 'model' and source_ref = %s")
    rows = conn.execute(q + (" and dimension_id = %s" if field else ""), (run_id, field) if field else (run_id,))
    return {(r["entity_id"], r["dimension_id"], r["value"]): r for r in rows}


def _run(conn, stage, fake=None, **kw):
    fake = fake or FakeJev(pick=_pick)
    kw.setdefault("progress", lambda s: None)
    return backfill.run(conn, stage, client=client(fake), **kw), fake


# ---------------------------------------------------------------- units

def test_machine_mail_is_one_case_per_template_sample_and_one_per_small_template(conn, ingestor):
    ids = _archive(conn, ingestor)
    cs = backfill.cases(conn, "machine")
    inv = [c for c in cs if c.kind == "template"]
    assert [c.anchor for c in inv] == [ids["invoices"][0], ids["invoices"][2], ids["invoices"][4]]  # first, middle, last
    assert [c.sample for c in inv] == [0, 1, 2] and len({c.key for c in inv}) == 1
    assert all(c.members == ids["invoices"] for c in inv)
    assert inv[1].load == {"kind": "pattern", "context_ids": [ids["invoices"][0], ids["invoices"][4]],
                           "pattern_key": inv[1].key.removeprefix("template:")}
    tail = {c.key: c for c in cs if c.kind == "tail"}
    codes = next(c for c in tail.values() if "banken" in c.key)
    assert codes.anchor == ids["codes"][1] and codes.members == ids["codes"]  # its latest, standing for both
    assert codes.load["kind"] == "pattern" and codes.load["context_ids"] == [ids["codes"][0]]
    welcome = next(c for c in tail.values() if "butiken" in c.key)
    assert welcome.members == [ids["welcome"]] and welcome.load == {"kind": "message"}
    # templates come first, then the tail newest first; person mail is not machine mail
    assert cs[0].kind == "template" and [c.key for c in cs[3:]] == [codes.key, welcome.key]
    assert not {m for c in cs for m in c.members} & set(ids["pia"] + [ids["letter"]])
    # since keeps the units with a message in the last N days; limit the first N units
    assert {c.key for c in backfill.cases(conn, "machine", since=30)} == {codes.key}
    assert len(backfill.cases(conn, "machine", limit=1)) == 3  # one unit: the template's three samples


def test_person_mail_is_one_case_per_thread_anchored_on_its_latest_incoming_message(conn, ingestor):
    ids = _archive(conn, ingestor)
    cs = {c.key: c for c in backfill.cases(conn, "person")}
    pia = next(c for c in cs.values() if c.anchor == ids["pia"][-1])
    assert pia.kind == "thread" and pia.members == ids["pia"] and ids["owners"] not in pia.members
    tid = conn.execute("select thread_id from message where id = %s", (ids["owners"],)).fetchone()["thread_id"]
    assert pia.key == f"thread:{tid}" and pia.load == {"kind": "thread", "thread_id": tid}
    letter = next(c for c in cs.values() if c.anchor == ids["letter"])
    assert letter.members == [ids["letter"]] and letter.load == {"kind": "message"}  # a thread of one
    rec = jev.record(pia.unit(conn), "context")
    assert rec["text"] == "Pia skriver 3" and rec["context"]["kind"] == "the rest of the thread (5 messages in all)"
    assert any("Tack, jag kollar." in line for line in rec["context"]["lines"])
    assert [c.anchor for c in backfill.cases(conn, "person")][:1] == [ids["pia"][-1]]  # newest first
    assert [c.anchor for c in backfill.cases(conn, "person", since=30)] == [ids["pia"][-1]]


def test_teams_is_one_case_per_two_hour_window_split_at_the_cap(conn, ingestor):
    ids = _archive(conn, ingestor)
    cs = backfill.cases(conn, "teams")
    chat = sorted((c for c in cs if c.members[0] in ids["chat"]), key=lambda c: c.members[0])
    assert [c.members for c in chat] == [ids["chat"][:3], ids["chat"][3:]]  # the 5 h gap starts a new window
    assert chat[0].anchor == ids["chat"][2]  # the latest incoming line (line 1 is the owner's)
    long = sorted((c for c in cs if c.members[0] in ids["long"]), key=lambda c: c.members[0])
    assert [len(c.members) for c in long] == [30, 5]  # split at TEAMS_WINDOW_CAP
    tid = conn.execute("select thread_id from message where id = %s", (ids["long"][0],)).fetchone()["thread_id"]
    assert long[1].key == f"window:{tid}:{ids['long'][30]}"
    assert long[0].load == {"kind": "window", "thread_id": tid, "window_first_id": ids["long"][0],
                            "window_last_id": ids["long"][29]}
    rec = jev.record(long[1].unit(conn), "context")
    assert rec["unit"] == "window" and len(rec["window"]["lines"]) == 5 and len(rec["window"]["before_window"]) == 3


# ---------------------------------------------------------------- records: the answer key's stay the same

def _old_item(conn, set_id, position):
    """gold.item() as it was before gold.unit() was taken out of it: the reference the answer
    key's records must still match byte for byte."""
    it = gold._item_row(conn, set_id, position)
    anchor = gold._blind_messages(conn, [it["message_id"]], body_max=gold.BODY_MAX, full=True)
    out = {"position": position, "unit": it["unit"], "message": anchor[0] if anchor else None, "context": [],
           "before": []}
    if it["unit"] == "thread" and it["thread_id"]:
        ids = [r["id"] for r in conn.execute(
            "select id from (select id, received_at, abs(extract(epoch from received_at - %s)) as d from message"
            " where thread_id = %s order by d nulls last, id limit %s) x order by received_at nulls first, id",
            (anchor[0]["received_at"] if anchor else None, it["thread_id"], gold.CONTEXT_MAX))]
        out["context"] = gold._blind_messages(conn, ids, body_max=gold.CONTEXT_BODY_MAX)
        out["thread_messages"] = conn.execute("select count(*)::int as n from message where thread_id = %s",
                                              (it["thread_id"],)).fetchone()["n"]
    elif it["unit"] == "pattern":
        out["context"] = gold._blind_messages(conn, list(it["context_ids"]), body_max=gold.CONTEXT_BODY_MAX)
        out["pattern"] = conn.execute("select message_count, thread_count, first_at, last_at from subject_pattern"
                                      " where pattern_key = %s", (it["pattern_key"],)).fetchone() or {}
    elif it["unit"] == "window":
        bounds = conn.execute("select received_at, id from message where id in (%s, %s) order by received_at, id",
                              (it["window_first_id"], it["window_last_id"])).fetchall()
        lo, hi = bounds[0], bounds[-1]
        ids = [r["id"] for r in conn.execute(
            "select id from message where thread_id = %s and (received_at, id) >= (%s, %s) and (received_at, id) <= (%s, %s)"
            " order by received_at, id", (it["thread_id"], lo["received_at"], lo["id"], hi["received_at"], hi["id"]))]
        before = [r["id"] for r in conn.execute(
            "select id from message where thread_id = %s and (received_at, id) < (%s, %s)"
            " order by received_at desc, id desc limit 3", (it["thread_id"], lo["received_at"], lo["id"]))][::-1]
        out["context"] = gold._blind_messages(conn, ids, body_max=gold.CONTEXT_BODY_MAX)
        out["before"] = gold._blind_messages(conn, before, body_max=gold.CONTEXT_BODY_MAX)
    return out


def test_the_answer_keys_records_are_byte_identical_after_the_unit_refactor(conn, ingestor):
    _full(conn, ingestor)
    sid = gold.sample(conn, n=30, seed=3)["set_id"]
    items = _items(conn, sid)
    assert {i["unit"] for i in items} >= {"thread", "pattern", "window", "message"}
    for i in items:
        for unit in jev.UNITS:
            new = jev.encode(jev.record(gold.item(conn, sid, i["position"]), unit))
            assert new == jev.encode(jev.record(_old_item(conn, sid, i["position"]), unit)), (i["position"], unit)


def test_a_backfill_case_on_the_same_messages_as_an_answer_key_item_gives_the_same_record(conn, ingestor):
    _full(conn, ingestor)
    sid = gold.sample(conn, n=30, seed=3)["set_id"]
    for i in _items(conn, sid):
        load = {"kind": i["unit"]}
        if i["unit"] == "thread":
            load["thread_id"] = i["thread_id"]
        elif i["unit"] == "pattern":
            load.update(context_ids=list(i["context_ids"]), pattern_key=i["pattern_key"])
        elif i["unit"] == "window":
            load.update(thread_id=i["thread_id"], window_first_id=i["window_first_id"],
                        window_last_id=i["window_last_id"])
        case = backfill.Case("x", "thread", "k", i["message_id"], [i["message_id"]], 0, load)
        assert jev.encode(jev.record(case.unit(conn), "context")) == \
            jev.encode(jev.record(gold.item(conn, sid, i["position"]), "context"))


# ---------------------------------------------------------------- the dry run and the guards

def test_a_dry_run_sends_nothing_reads_no_key_and_writes_nothing(conn, ingestor, database, keychain):
    _archive(conn, ingestor)
    before = {t: conn.execute(f"select count(*)::int as n from {t}").fetchone()["n"]
              for t in ("model_run", "enrich_case", "enrich_prediction", "assignment")}
    fake = FakeJev(pick=_pick)
    with db.connect(database, autocommit=True) as ac:  # as the command line connects
        res = backfill.run(ac, "machine", client=client(fake), dry_run=True, max_cases=4)
    assert fake.requests == [] and keychain == []
    assert {t: conn.execute(f"select count(*)::int as n from {t}").fetchone()["n"] for t in before} == before
    assert res["to_send"] == 5 and res["units"] == {"template": {"units": 1, "cases": 3, "messages": 5},
                                                    "tail": {"units": 2, "cases": 2, "messages": 3}}
    assert res["over_max_cases"] and not res["over_budget"]
    assert res["request"]["state"]["recipient"] == jev.RECIPIENT and res["request"]["state"]["unit"] == "context"
    assert res["request"]["questions"] == jev.question_sets(jev.taxonomy_from_db(conn))["email"]
    e = res["estimate"]
    assert e["measured_input_tokens"] == 5 * backfill.MEASURED_TOKENS_PER_CASE and e["input_tokens"] > 5 * 3000
    assert e["seconds"] == round(5 / backfill.MEASURED_CASES_PER_SECOND, 1)
    text = backfill.format_dry_run(res)
    assert text.startswith("DRY RUN: nothing is sent") and "REFUSED: over --max-cases" in text
    assert json.loads(text[text.index("\n{") + 1:]) == res["request"]


def test_the_dry_run_works_in_a_read_only_transaction(conn, ingestor, database):
    _archive(conn, ingestor)
    with db.connect(database, autocommit=True) as ac:
        ac.execute("set default_transaction_read_only = on")  # as on the real archive
        for stage in backfill.STAGES:
            assert backfill.run(ac, stage, dry_run=True)["to_send"] > 0


def test_the_cost_guard_refuses_too_many_cases_or_an_estimate_over_the_budget(conn, ingestor, keychain):
    _archive(conn, ingestor)
    fake = FakeJev(pick=_pick)
    with pytest.raises(backfill.BackfillError, match="5 cases to send is over --max-cases 4"):
        backfill.run(conn, "machine", client=client(fake), max_cases=4)
    with pytest.raises(backfill.BackfillError, match=r"is over --budget \$0.00"):
        backfill.run(conn, "machine", client=client(fake), budget=0.0001)
    assert fake.requests == [] and keychain == []
    assert conn.execute("select count(*)::int as n from model_run").fetchone()["n"] == 0
    assert backfill.MAX_CASES == 60_000


def test_a_run_stops_once_it_has_spent_its_budget_and_resumes_where_it_stopped(conn, ingestor, keychain):
    _archive(conn, ingestor)
    fake = FakeJev(pick=_pick, input_tokens=10_000_000)  # $0.42 a case
    res, _ = _run(conn, "machine", fake, budget=0.5, chunk=2)
    assert len(fake.requests) == 2 and res["stored"] == 2 and "budget $0.50 is spent" in res["error"]
    res2, fake2 = _run(conn, "machine", run_id=res["run_id"], chunk=2)
    assert len(fake2.requests) == 3 and res2["stored"] == 3 and "error" not in res2
    assert conn.execute("select count(*)::int as n from enrich_case where run_id = %s and error is null",
                        (res["run_id"],)).fetchone()["n"] == 5
    p = conn.execute("select params from model_run where id = %s", (res["run_id"],)).fetchone()["params"]
    assert p["invocations"] == 2 and p["cases_done"] == 5 and p["input_tokens"] == 2 * 10_000_000 + 3 * 5000


def test_a_refused_key_stops_the_run_and_keeps_what_was_not_sent_for_a_resume(conn, ingestor, keychain):
    _archive(conn, ingestor)
    fake = FakeJev(pick=_pick, statuses=[401])
    res = backfill.run(conn, "machine", client=client(fake, concurrency=1), run_id="bf-auth", progress=lambda s: None)
    assert "refused the key" in res["error"] and res["stored"] == 0 and len(fake.requests) == 1
    errors = [r["error"] for r in conn.execute("select error from enrich_case where run_id = 'bf-auth'")]
    assert errors == ["not answered (the run stopped)"] * 5
    res2, fake2 = _run(conn, "machine", run_id="bf-auth")
    assert len(fake2.requests) == 5 and res2["stored"] == 5


def test_a_resumed_run_retries_only_what_failed_and_refuses_another_stage(conn, ingestor, keychain):
    _archive(conn, ingestor)
    fake = FakeJev(pick=_pick, statuses=[500, 500])
    res = backfill.run(conn, "machine", client=client(fake, max_retries=0), run_id="bf-test-1", progress=lambda s: None)
    assert res["stored"] == 3 and len(res["failed"]) == 2
    failed = conn.execute("select unit_key, error from enrich_case where run_id = 'bf-test-1' and error is not null"
                          ).fetchall()
    assert len(failed) == 2 and all("HTTP 500" in f["error"] for f in failed)
    res2, fake2 = _run(conn, "machine", run_id="bf-test-1")
    assert len(fake2.requests) == 2 and res2["stored"] == 2 and res2["already_done"] == 3
    assert conn.execute("select count(*)::int as n from enrich_case where run_id = 'bf-test-1'").fetchone()["n"] == 5
    res3, fake3 = _run(conn, "machine", run_id="bf-test-1")  # nothing left: nothing sent
    assert fake3.requests == [] and res3["to_send"] == 0
    with pytest.raises(backfill.BackfillError, match="made with another stage"):
        _run(conn, "person", run_id="bf-test-1")
    with pytest.raises(backfill.BackfillError, match="does not start with rule or prepass"):
        _run(conn, "machine", run_id="rule:mine")


# ---------------------------------------------------------------- proposals

def test_a_template_propagates_a_field_only_where_its_three_samples_agree(conn, ingestor, keychain):
    ids = _archive(conn, ingestor)
    res, _ = _run(conn, "machine")
    run_id = res["run_id"]
    inv = ids["invoices"]
    types = {e for (e, f, v) in _rows(conn, run_id, "type") if v == "invoice"}
    assert types == set(inv)  # all three said invoice: the whole template
    values = {(e, v) for (e, f, v) in _rows(conn, run_id, "value") if e in inv}
    assert values == {(inv[0], "transient"), (inv[2], "record_financial"), (inv[4], "transient")}  # split: samples only
    r = _rows(conn, run_id, "type")
    own, prop = r[(inv[2], "type", "invoice")], r[(inv[1], "type", "invoice")]
    assert own["status"] == prop["status"] == "proposed"
    assert own["evidence"]["propagated"] is False and prop["evidence"]["propagated"] is True
    assert prop["evidence"]["agree"] is True and len(prop["evidence"]["cases"]) == 3
    assert prop["evidence"]["unit"].startswith("template:faktura@telia.se|faktura # från")
    assert prop["evidence"]["question_version"] == res["question_version"]
    assert abs(own["confidence"] - 0.93) < 1e-6 and next(iter(own["evidence"]["scores"])) == "invoice"
    assert len(own["evidence"]["scores"]) == 3 and own["evidence"]["margin"] > 0.8 and own["evidence"]["decided"]
    # the tail: a template of two gives its answer to both, one of one to its message
    codes = {e for (e, f, v) in _rows(conn, run_id, "origin")} & set(ids["codes"])
    assert codes == set(ids["codes"])
    assert res["propagation"]["templates"]["type"] == {"agreed": 1, "split": 0}
    assert res["propagation"]["templates"]["value"] == {"agreed": 0, "split": 1}
    # ask and route: none writes nothing
    assert not _rows(conn, run_id, "ask") and not _rows(conn, run_id, "route")


def test_a_thread_and_a_window_give_their_answers_to_their_messages_and_gates_apply(conn, ingestor, keychain):
    ids = _archive(conn, ingestor)
    res, _ = _run(conn, "person")
    ask = _rows(conn, res["run_id"], "ask")
    assert {e for (e, f, v) in ask if v == "question"} == set(ids["pia"])  # not the owner's own reply
    route = _rows(conn, res["run_id"], "route")
    assert {e for (e, f, v) in route} == set(ids["pia"])
    assert ask[(ids["pia"][0], "ask", "question")]["evidence"]["propagated"] is True
    assert ask[(ids["pia"][-1], "ask", "question")]["evidence"]["propagated"] is False
    # the letter: a route, but its topic (Family) is not work, so the gate forces route to none
    g = conn.execute("select p.selected, p.gate, p.ungated from enrich_prediction p join enrich_case c on c.id = p.case_id"
                     " where c.anchor_id = %s and p.field = 'route'", (ids["letter"],)).fetchone()
    assert g == {"selected": [], "gate": ["route:topic"], "ungated": ["Security"]}
    assert not any(e == ids["letter"] for (e, f, v) in route)
    res_t, fake_t = _run(conn, "teams")
    assert all(b["questions"] == jev.question_sets(jev.taxonomy_from_db(conn))["teams"] for b in fake_t.requests)
    rows = _rows(conn, res_t["run_id"])
    assert not any(f == "origin" for (_, f, _) in rows)  # a window's origin is fixed, never proposed
    assert {e for (e, f, v) in rows if f == "type"} == set(ids["chat"] + ids["long"])  # every line, the owner's too


def test_a_rerun_replaces_its_own_proposals_and_touches_nothing_else(conn, ingestor, keychain):
    ids = _archive(conn, ingestor)
    rules.assign(conn, [ids["pia"][0]], "topic", "Work/Customers")                    # the owner's own
    other = conn.execute("insert into model_run (id, model, purpose) values ('other-run', 'x', 'x') returning id").fetchone()
    conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, status)"
                 " values (%s, 'topic', 'Travel', 'model', %s, 'proposed')", (ids["pia"][0], other["id"]))
    theirs = "select id, entity_id, dimension_id, value, source_kind, source_ref, status from assignment" \
             " where not (source_kind = 'model' and source_ref like 'jev-backfill-%%') order by id"
    before = conn.execute(theirs).fetchall()
    assert any(r["source_ref"].startswith("prepass:") for r in before)
    res, _ = _run(conn, "person")
    first = _rows(conn, res["run_id"])
    # accept one row, reject another: a rerun keeps both and proposes neither again
    backfill.accept(conn, res["run_id"], {"ask": 0.8})
    conn.execute("update assignment set status = 'rejected' where source_ref = %s and dimension_id = 'route'"
                 " and entity_id = %s", (res["run_id"], ids["pia"][0]))
    res2, fake2 = _run(conn, "person", run_id=res["run_id"])
    assert fake2.requests == [] and res2["propagation"]["replaced"] > 0
    second = _rows(conn, res["run_id"])
    assert second.keys() == first.keys()  # the same rows, not twice
    assert {r["status"] for k, r in second.items() if k[1] == "ask"} == {"active"}
    assert second[(ids["pia"][0], "route", "Customers & Business")]["status"] == "rejected"
    assert conn.execute(theirs).fetchall() == before  # human, rule, pre-pass and other runs untouched
    rules.run_all(conn)  # a rules run does not touch the run's rows either
    assert _rows(conn, res["run_id"]).keys() == first.keys()


# ---------------------------------------------------------------- accept

def test_accept_promotes_over_the_threshold_with_a_margin_and_a_later_accept_can_change_it(conn, ingestor, keychain):
    ids = _archive(conn, ingestor)
    res, _ = _run(conn, "machine")
    rid = res["run_id"]
    dry = backfill.accept(conn, rid, {"type": 0.90, "value": 0.85}, dry_run=True)
    assert dry["fields"]["type"]["promoted"] == 8 and dry["fields"]["type"]["promoted_values"]["invoice"] == 5
    assert {r["status"] for r in _rows(conn, rid).values()} == {"proposed"}  # the dry run rolled back
    out = backfill.accept(conn, rid, {"type": 0.90, "value": 0.85})
    assert out["decided_by"] == "policy:type>=0.90,value>=0.85,margin>=0.15"
    t = _rows(conn, rid, "type")
    assert t[(ids["invoices"][1], "type", "invoice")]["status"] == "active"             # 0.93 ≥ 0.90
    assert t[(ids["invoices"][1], "type", "invoice")]["decided_by"] == out["decided_by"]
    assert {r["status"] for r in _rows(conn, rid, "topic").values()} == {"proposed"}     # not given: left alone
    # the same threshold under another policy is re-stamped; a stricter one puts back what no longer qualifies
    assert backfill.accept(conn, rid, {"type": 0.90})["fields"]["type"]["restamped"] == 8
    again = backfill.accept(conn, rid, {"type": 0.92})
    assert (again["fields"]["type"]["demoted"], again["fields"]["type"]["active"]) == (3, 5)  # 0.90 < 0.92 ≤ 0.93
    again = backfill.accept(conn, rid, {"type": 0.95})
    assert again["fields"]["type"]["demoted"] == 5 and again["fields"]["type"]["active"] == 0
    assert _rows(conn, rid, "type")[(ids["invoices"][1], "type", "invoice")]["status"] == "proposed"
    assert backfill.accept(conn, rid, {"type": 0.5}, margin=0.99)["fields"]["type"]["active"] == 0  # the margin too
    backfill.accept(conn, rid, {"origin": 0.9, "type": 0.9})
    assert backfill.unaccept(conn, rid, dry_run=True)["fields"] == {"origin": 8, "type": 8, "value": 6}
    undone = backfill.unaccept(conn, rid)
    assert undone["fields"] == {"origin": 8, "type": 8, "value": 6}
    assert {r["status"] for r in _rows(conn, rid).values()} == {"proposed"}
    assert all(r["decided_by"] is None for r in _rows(conn, rid).values())
    with pytest.raises(backfill.BackfillError, match="no enrichment backfill run"):
        backfill.accept(conn, "nope", {"type": 0.9})


def test_an_accepted_model_value_never_beats_a_rule_or_pre_pass_value(conn, ingestor, keychain):
    ids = _archive(conn, ingestor)
    pia = ids["pia"][0]
    pre = conn.execute("select value, source_ref from assignment where entity_id = %s and dimension_id = 'origin'",
                       (pia,)).fetchone()
    assert pre["source_ref"].startswith("prepass:origin.") and pre["value"] == "person"
    rules.save(conn, rules.Rule("pia-type", "Pia is notifications", [{"field": "from_domain", "op": "is",
                                                                       "value": "nordvik.se"}],
                                {"dimension": "type", "value": "notification"}))
    rules.run_all(conn)
    res, _ = _run(conn, "person", FakeJev(pick=lambda b: {**_pick(b), "origin": ("marketing", 0.97)}))
    backfill.accept(conn, res["run_id"], {"origin": 0.85, "type": 0.5, "topic": 0.85})
    eff = {r["dimension_id"]: r for r in conn.execute(
        "select dimension_id, value, source_kind from effective_message_assignment where message_id = %s", (pia,))}
    assert (eff["origin"]["value"], eff["origin"]["source_kind"]) == ("person", "rule")        # the pre-pass wins
    assert (eff["type"]["value"], eff["type"]["source_kind"]) == ("notification", "rule")      # the rule wins
    assert (eff["topic"]["value"], eff["topic"]["source_kind"]) == ("Work/IT operations", "model")  # nothing else
    # the other rankings agree: the answer key's (and step A's), and People / Automated in Messages
    o = conn.execute(f"with {gold._ORIGIN_CTE} select value from o where message_id = %(m)s",
                     {"noise": [], "machine": [], "person": [], "m": pia}).fetchone()
    assert o["value"] == "person"
    machine = conn.execute(f"select {search.machine_sql()} as x from message m where m.id = %s", (pia,)).fetchone()
    assert machine["x"] is False
    # a human decision on the thread still beats an accepted model value on the message
    tid = conn.execute("select thread_id from message where id = %s", (pia,)).fetchone()["thread_id"]
    rules.assign(conn, [tid], "topic", "Work/Customers")
    assert conn.execute("select value from effective_message_assignment where message_id = %s"
                        " and dimension_id = 'topic'", (pia,)).fetchone()["value"] == "Work/Customers"


def test_accept_leaves_an_ask_proposed_beside_a_human_or_rule_ask(conn, ingestor, keychain):
    ids = _archive(conn, ingestor)
    rules.assign(conn, [ids["pia"][0]], "ask", "action")
    res, _ = _run(conn, "person")
    out = backfill.accept(conn, res["run_id"], {"ask": 0.8})
    assert out["fields"]["ask"]["promoted"] == 3 and out["fields"]["ask"]["still_proposed"] == 1
    assert _rows(conn, res["run_id"], "ask")[(ids["pia"][0], "ask", "question")]["status"] == "proposed"
    vals = {r["value"] for r in conn.execute("select value from effective_message_assignment where message_id = %s"
                                             " and dimension_id = 'ask'", (ids["pia"][0],))}
    assert vals == {"action"}


def test_proposals_stay_out_of_the_effective_values_until_accepted(conn, ingestor, keychain):
    ids = _archive(conn, ingestor)
    res, _ = _run(conn, "machine")
    q = "select value from effective_message_assignment where message_id = %s and dimension_id = 'type'"
    assert conn.execute(q, (ids["invoices"][1],)).fetchone() is None
    backfill.accept(conn, res["run_id"], {"type": 0.9})
    assert conn.execute(q, (ids["invoices"][1],)).fetchone()["value"] == "invoice"


# ---------------------------------------------------------------- the report and the command line

def test_the_report_counts_cases_decisions_values_proposals_and_templates(conn, ingestor, keychain):
    _archive(conn, ingestor)
    res, _ = _run(conn, "machine")
    rep = backfill.report(conn, res["run_id"])
    assert {(r["unit_kind"], r["done"], r["units"]) for r in rep["cases"]} == {("template", 3, 1), ("tail", 2, 2)}
    f = {r["field"]: r for r in rep["fields"]}
    assert f["type"]["n"] == 5 and f["type"]["decided"] == 5 and f["ask"]["selected"] == 0
    assert rep["distribution"]["value"] == {"knowledge": 2, "transient": 2, "record_financial": 1}  # per case
    assert rep["distribution"]["type"] == {"invoice": 3, "conversation": 2}
    p = {r["field"]: r for r in rep["proposals"]}
    assert p["type"]["messages"] == 8 and p["type"]["propagated"] == 3
    t = {r["field"]: r for r in rep["templates"]}
    assert (t["type"]["agreed"], t["value"]["split"]) == (1, 1)
    assert rep["cost"]["cases_done"] == 5 and rep["cost"]["input_tokens"] == 5 * 5000
    text = backfill.format_report(rep)
    assert "stage machine" in text and "agreed" in text


def test_the_command_line_runs_a_dry_run_a_stage_its_report_and_accept(
        conn, ingestor, database, vault, monkeypatch, capsys, keychain):
    _archive(conn, ingestor)
    fake = FakeJev(pick=_pick)
    real = jev.JevClient
    monkeypatch.setattr(jev, "JevClient", lambda **kw: real(transport=fake.transport, sleep=_no_sleep, **kw))
    monkeypatch.setenv("TALOS_DSN", database)
    monkeypatch.setenv("TALOS_HOME", str(vault.root.parent))
    monkeypatch.setattr(cli, "_logging", lambda settings, verbose: None)
    cli.main(["enrich", "jev", "backfill", "--stage", "teams", "--dry-run"])
    out = capsys.readouterr().out
    assert out.startswith("DRY RUN") and "window" in out and fake.requests == []
    cli.main(["enrich", "jev", "backfill", "--stage", "teams", "--run", "bf-cli-1", "--concurrency", "2"])
    out = capsys.readouterr().out
    assert "run bf-cli-1: 4 stored, 0 failed" in out and "talos enrich accept --run bf-cli-1 --dry-run" in out
    cli.main(["enrich", "jev", "backfill-report", "--run", "bf-cli-1"])
    assert "Backfill run bf-cli-1: stage teams" in capsys.readouterr().out
    cli.main(["enrich", "accept", "--run", "bf-cli-1", "--dry-run"])
    out = capsys.readouterr().out
    assert out.startswith("DRY RUN (rolled back)") and "promoted" in out
    cli.main(["enrich", "accept", "--run", "bf-cli-1", "--type", "0.5"])
    assert "type    ≥ 0.50" in capsys.readouterr().out
    cli.main(["enrich", "unaccept", "--run", "bf-cli-1"])
    assert "back to proposed: topic 40, type 40, value 40" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["enrich", "jev", "backfill"])
    assert "usage: talos enrich jev backfill --stage" in capsys.readouterr().err
