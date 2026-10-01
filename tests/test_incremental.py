"""Incremental enrichment (talos.incremental): what is new, reuse, the three questions, proposals,
accept by the stored policy, boundaries and the structure plan, the sync hook, its throttle, budget
and Argus check-in.

Never the real Jev: every request goes to test_jev's fake TypeSafe endpoint (httpx.MockTransport).
Invented mail only.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from test_accept import _gate
from test_backfill import TELIA, PIA, _archive, _pick, client
from test_enrich import ME, mail
from test_gold import _teams
from test_jev import FakeJev, _no_sleep, keychain  # noqa: F401  (keychain is a fixture)

from talos import argus, backfill, cli, enrich, focus, gold, incremental, jev, structure

pytestmark = pytest.mark.usefixtures("taxonomy_loaded")

AUTO = {"Auto-Submitted": "auto-generated"}
OLLE = "Olle Sten <olle.sten@nordvik.se>"
EMAIL_QUESTIONS = ["ask", "ask:deadline", "origin", "route", "topic", "type", "value"]


def quiet(_):
    return None


def _pick_all(body: dict) -> dict:
    """The backfill's answers, and for the focused questions: invoices are transactions and
    transient, Pia's and Olle's mail questions with context value, the rest fyi."""
    qs = list(body["questions"])
    text = body["state"].get("text") or ""
    person = "Pia skriver" in text or "Olle" in text
    if qs == ["kind"]:
        return {"kind": ("transaction", 0.95) if "Faktura" in text else ("question_request", 0.9) if person
                else ("fyi", 0.9)}
    if qs == ["value"]:
        return {"value": ("transient", 0.9) if "Faktura" in text else ("context", 0.88)}
    if "Olle" in text:
        return {"origin": ("person", 0.97), "type": ("question", 0.9), "topic": ("Work/IT operations", 0.9),
                "ask": ("question", 0.9), "route": ("IT Operations", 0.86), "value": ("context", 0.8)}
    return _pick(body)


def _judged(conn, ingestor, *, focus_value=True, accept=True) -> dict:
    """The backfill's archive judged as the real one was: the three stages, the focused kind and
    value runs over every case, and the two-level policy applied; sender_kind's focused question
    checked on the answer key too, as it is on the real one."""
    ids = _archive(conn, ingestor)
    for st in backfill.STAGES:
        backfill.run(conn, st, client=client(FakeJev(pick=_pick_all)), progress=quiet)
    for f in ("sender_kind", "kind", "value"):
        _gate(conn, f)
    focus.run(conn, "kind", "all", client=client(FakeJev(pick=_pick_all)), progress=quiet)
    if focus_value:
        focus.run(conn, "value", "all", client=client(FakeJev(pick=_pick_all)), progress=quiet)
    if accept:
        backfill.accept(conn, backfill.runs_of(conn, "all"), backfill.TWO_LEVEL)
    conn.commit()
    return ids


def _arrive(conn):
    """What talos sync does with new mail before the hook: the pre-pass (patterns, origin)."""
    enrich.prepass(conn)
    conn.commit()


def _new(conn, fake=None, **kw):
    fake = fake or FakeJev(pick=_pick_all)
    return incremental.run(conn, client=client(fake), progress=quiet, **kw), fake


def _model(conn, mid) -> dict[str, dict]:
    """The message's live model rows (proposed or active), by field."""
    return {r["dimension_id"]: r for r in conn.execute(
        "select dimension_id, value, status, source_ref, confidence, evidence, decided_by from assignment"
        " where entity_id = %s and source_kind = 'model' and status in ('proposed', 'active')", (mid,))}


def _eff(conn, mid, dim):
    r = conn.execute("select value, source_kind from effective_message_assignment where message_id = %s"
                     " and dimension_id = %s", (mid, dim)).fetchone()
    return (r["value"], r["source_kind"]) if r else None


def _invoice(ingestor, n=9, days_ago=1):
    return mail(ingestor, frm=TELIA, subject=f"Faktura {2026_10 + n} från Telia", body=f"Faktura nr {n}",
                headers=AUTO, days_ago=days_ago)


# ---------------------------------------------------------------- what is new

def test_new_mail_is_incoming_unjudged_mail_in_no_case_and_never_the_owners_own(conn, ingestor, keychain):
    ids = _judged(conn, ingestor)
    olle = mail(ingestor, frm=OLLE, subject="Brandväggen", body="Olle undrar om brandväggen", thread="olle", days_ago=1)
    mine = mail(ingestor, frm=ME, to=OLLE, subject="Re: Brandväggen", body="Jag kollar.", thread="olle", days_ago=1)
    old = mail(ingestor, frm="Gammal Vän <gammal@example.org>", subject="Minns du?", body="x", thread="g", days_ago=40)
    # a case's member whose proposals are gone is still judged: it is in a case
    conn.execute("delete from assignment where entity_id = %s and source_kind = 'model'", (ids["codes"][0],))
    _arrive(conn)
    assert conn.execute("select direction from message where id = %s", (mine,)).fetchone()["direction"] == "out"
    assert {c["id"] for c in incremental.plan(conn).candidates} == {olle}
    assert {c["id"] for c in incremental.plan(conn, backlog=True).candidates} == {olle, old}  # older than 7 days
    census = {(r["account_id"], r["medium"]): r for r in incremental.census(conn)}[("gmail", "email")]
    assert census["sent"] >= 1 and census["unjudged"] == 3 and census["unjudged_recent"] == 1  # old, codes[0], olle


# ---------------------------------------------------------------- reuse

def test_a_template_whose_three_samples_agree_gives_new_mail_its_answers_without_asking_jev(conn, ingestor, keychain):
    _judged(conn, ingestor)
    new = _invoice(ingestor)
    _arrive(conn)
    res, fake = _new(conn)
    assert fake.requests == [] and res["asked"]["cases"] == 0
    assert res["reused"] == {"units": 1, "messages": 1, "by_kind": {"template": 1}}
    rows = _model(conn, new)
    assert {f: r["value"] for f, r in rows.items() if f in ("origin", "type", "topic", "value", "kind")} == {
        "origin": "notification", "type": "invoice", "topic": "Finance/Payments", "value": "transient",
        "kind": "transaction"}
    src = rows["type"]["evidence"]["propagated_from"]
    assert rows["type"]["evidence"]["propagated"] and src["kind"] == "template" and src["unit"].startswith("template:")
    assert rows["value"]["evidence"]["propagated_from"]["kind"] == "template"  # value from the focused run, agreed
    assert all(r["source_ref"] == res["runs"][0] for r in rows.values())
    # accepted by the stored policy, boundaries included
    assert rows["type"]["status"] == "active" and rows["type"]["decided_by"].startswith("policy:origin>=0.70")
    assert _eff(conn, new, "sender_kind") == ("machine", "model") and _eff(conn, new, "kind") == ("transaction", "model")
    assert _eff(conn, new, "sphere") == ("personal", "model") and _eff(conn, new, "keep") == ("short_lived", "model")
    # it is judged now: a second run finds nothing new
    again, fake2 = _new(conn)
    assert again["selected"] == 0 and again["runs"] == [] and fake2.requests == []


def test_a_template_that_split_asks_jev_for_the_new_mail_with_its_samples_as_context(conn, ingestor, keychain):
    _judged(conn, ingestor, focus_value=False)  # the invoices split on value (nr 2 is a record)
    new = _invoice(ingestor)
    _arrive(conn)
    res, fake = _new(conn)
    assert res["reused"]["units"] == 0 and res["asked"]["why"] == {"template split": 1}
    assert len(fake.requests) == 4
    ctx = fake.requests[0]["state"]["context"]
    assert fake.requests[0]["state"]["text"] == "Faktura nr 9" and any("Faktura nr" in line for line in ctx["lines"])
    case = conn.execute("select unit_kind, unit_key, member_ids from enrich_case where run_id = %s",
                        (res["runs"][0],)).fetchone()
    assert case["unit_kind"] == "tail" and case["unit_key"].endswith(f"~{new}") and case["member_ids"] == [new]
    assert backfill.base_key(case["unit_key"]).startswith("tail:faktura@telia.se|")


def test_a_new_message_in_a_thread_takes_its_answers_until_the_thread_grew_by_three(conn, ingestor, keychain):
    ids = _judged(conn, ingestor)
    n1 = mail(ingestor, frm=PIA, subject="Re: Offert", body="Pia skriver 4", thread="pia", days_ago=1)
    _arrive(conn)
    res, fake = _new(conn)
    assert fake.requests == [] and res["reused"]["by_kind"] == {"thread": 1}
    rows = _model(conn, n1)
    assert rows["ask"]["value"] == "question" and rows["ask"]["evidence"]["propagated_from"]["kind"] == "thread"
    assert rows["ask"]["evidence"]["propagated_from"]["grown"] == 1
    # the owner's reply and hers: three messages since the thread's case, so it is asked again, whole
    mail(ingestor, frm=ME, to=PIA, subject="Re: Offert", body="Tack, jag återkommer.", thread="pia", days_ago=1)
    n2 = mail(ingestor, frm=PIA, subject="Re: Offert", body="Pia skriver 5", thread="pia", days_ago=1)
    _arrive(conn)
    res2, fake2 = _new(conn)
    assert res2["asked"]["why"] == {"grown": 1} and fake2.requests[0]["state"]["text"] == "Pia skriver 5"
    case = conn.execute("select unit_key, anchor_id, member_ids from enrich_case where run_id = %s",
                        (res2["runs"][0],)).fetchone()
    assert case["anchor_id"] == n2 and set(case["member_ids"]) == set(ids["pia"]) | {n1, n2}
    # the newest run's answer supersedes the backfill's for the thread's older messages
    old = conn.execute("select status, decided_by from assignment where entity_id = %s and dimension_id = 'ask'"
                       " and source_kind = 'model' and source_ref like 'jev-backfill-person%%'", (ids["pia"][0],)).fetchone()
    assert old["status"] == "superseded" and old["decided_by"] == f"superseded:{res2['runs'][0]}"
    assert _model(conn, ids["pia"][0])["ask"]["source_ref"] == res2["runs"][0]


def test_new_teams_lines_take_their_windows_answers_or_are_asked_as_a_new_window(conn, ingestor, keychain):
    ids = _judged(conn, ingestor)
    joins = _teams(conn, ingestor, "chatA", [25])[0]     # five minutes after the first window's last line
    alone = _teams(conn, ingestor, "chatA", [900])[0]    # ten hours after the second window: a new one
    conn.commit()
    res, fake = _new(conn)
    assert res["reused"]["by_kind"] == {"window": 1} and res["asked"]["why"] == {"new window": 1}
    assert _model(conn, joins)["type"]["evidence"]["propagated_from"]["unit"].startswith("window:")
    assert [r["state"]["unit"] for r in fake.requests] == ["window"] * 3
    assert "origin" not in fake.requests[0]["questions"]  # a window's origin is fixed, as in the backfill
    assert _eff(conn, alone, "sender_kind") == ("people", "model")
    assert ids["chat"][0] not in {c["id"] for c in incremental.plan(conn).candidates}


def test_an_applied_answer_key_group_gives_the_owners_answers_to_its_new_mail_and_jev_is_not_asked(conn, ingestor, keychain):
    ids = _judged(conn, ingestor)
    sid = conn.execute("insert into gold_set (name, seed, target, params) values ('g', 7, 1, %s) returning id",
                       (json.dumps({"label_fields": ["origin", "kind", "topic", "ask", "value", "route"]}),)).fetchone()["id"]
    g = {"key": "noreply@butiken.se", "sender": "noreply@butiken.se", "system": None, "stage": "machine"}
    conn.execute("insert into gold_item (set_id, position, message_id, stratum, reason, unit, info)"
                 " values (%s, 1, %s, 'machine', 'r', 'message', %s)", (sid, ids["welcome"],
                                                                      json.dumps({"sender_group": g})))
    for f, v in (("origin", ["marketing"]), ("kind", ["offer"]), ("topic", ["Shopping"]), ("value", ["noise"])):
        gold.save_label(conn, sid, 1, f, v)
    conn.execute("insert into gold_group_case (item_id, case_id) select i.id, c.id from gold_item i, enrich_case c"
                 " where i.set_id = %s and c.unit_key like 'tail:noreply@butiken.se|%%'", (sid,))
    conn.commit()
    new = mail(ingestor, frm="Butiken <noreply@butiken.se>", subject="Rea på allt", body="Rea!", headers=AUTO,
               days_ago=1)
    _arrive(conn)
    res, fake = _new(conn)
    assert res["groups"]["messages"] == 0 and len(fake.requests) == 4  # not applied yet: an ordinary new tail
    new2 = mail(ingestor, frm="Butiken <noreply@butiken.se>", subject="Nytt i butiken", body="Nyheter", headers=AUTO,
                days_ago=1)
    gold.propagate_groups(conn, sid, dry_run=False)  # the owner gives their answer to the group
    _arrive(conn)
    res, fake = _new(conn)
    assert res["groups"] == {"groups": 1, "messages": 1, "values": 4} and fake.requests == []
    row = conn.execute("select value, source_ref, evidence from assignment where entity_id = %s and dimension_id = 'kind'"
                       " and source_kind = 'human'", (new2,)).fetchone()
    assert row["value"] == "offer" and row["source_ref"] == f"gold:{sid}:1"
    assert row["evidence"]["propagated_from"] == {"set": sid, "item": 1, "item_id": row["evidence"]["propagated_from"]["item_id"]}
    assert new not in {c["id"] for c in incremental.plan(conn).candidates}


# ---------------------------------------------------------------- asked: the full set, then sender_kind, kind and value

def test_a_new_case_is_asked_the_full_set_then_sender_kind_kind_and_value_as_the_focused_runs_asked(conn, ingestor,
                                                                                                   keychain):
    _judged(conn, ingestor)
    olle = mail(ingestor, frm=OLLE, subject="Brandväggen", body="Olle undrar: kan du kolla brandväggen?",
                thread="olle", days_ago=1)
    _arrive(conn)
    res, fake = _new(conn)
    assert [sorted(r["questions"]) for r in fake.requests] == [EMAIL_QUESTIONS, ["sender_kind"], ["kind"], ["value"]]
    for f, r in zip(("sender_kind", "kind", "value"), fake.requests[1:]):
        assert r["questions"] == focus.question_sets(focus.dims_for(conn, f), f)["email"]  # exactly the focus run's
    full, sk_run, kind_run, value_run = res["runs"]
    assert sk_run == f"{full}-sender_kind" and kind_run == f"{full}-kind" and value_run == f"{full}-value"
    purposes = {r["id"]: r for r in conn.execute("select id, purpose, params from model_run where id = any(%s)",
                                                 (res["runs"],))}
    assert {r["purpose"] for r in purposes.values()} == {"enrich-incremental"}
    assert purposes[kind_run]["params"]["focus"]["field"] == "kind"
    rows = _model(conn, olle)
    assert rows["sender_kind"]["source_ref"] == sk_run                        # asked, no longer derived from origin
    assert rows["kind"]["value"] == "question_request" and rows["kind"]["source_ref"] == kind_run
    assert rows["value"]["value"] == "context" and rows["value"]["source_ref"] == value_run
    assert rows["keep"]["source_ref"] == value_run and rows["type"]["source_ref"] == full
    # the full run's own kind and value (derived and asked) are superseded by the focused answers
    sup = {r["dimension_id"] for r in conn.execute(
        "select dimension_id from assignment where entity_id = %s and source_ref = %s and status = 'superseded'",
        (olle, full))}
    assert {"sender_kind", "kind", "value", "keep"} <= sup
    assert _eff(conn, olle, "sender_kind") == ("people", "model") and _eff(conn, olle, "kind") == ("question_request", "model")
    assert rows["ask"]["value"] == "question" and rows["ask"]["status"] == "proposed"  # the policy leaves ask proposed


def test_a_focused_question_the_answer_key_never_had_is_not_asked(conn, ingestor, keychain):
    _judged(conn, ingestor)
    conn.execute("delete from model_run where id = 'gold-gate-value'")
    conn.commit()
    mail(ingestor, frm=OLLE, subject="Brandväggen", body="Olle undrar", thread="olle", days_ago=1)
    _arrive(conn)
    res, fake = _new(conn)
    assert [sorted(r["questions"]) for r in fake.requests] == [EMAIL_QUESTIONS, ["sender_kind"], ["kind"]]
    assert any("focused value not asked" in n for n in res["notes"])


# ---------------------------------------------------------------- accept, boundaries, structure

def test_new_values_are_accepted_by_the_policy_last_applied_or_by_enrich_json(conn, ingestor, keychain):
    _judged(conn, ingestor, accept=False)
    olle = mail(ingestor, frm=OLLE, subject="Brandväggen", body="Olle undrar", thread="olle", days_ago=1)
    _arrive(conn)
    res, _ = _new(conn)
    assert res["policy"] is None and "accept" not in res and any("nothing accepted" in n for n in res["notes"])
    assert {r["status"] for r in _model(conn, olle).values()} == {"proposed"}
    # the owner applies a policy to the archive; the next run accepts the new values with it (and the runs left over)
    backfill.accept(conn, backfill.runs_of(conn, "all"), {**backfill.TWO_LEVEL, "type": 0.95})
    conn.commit()
    olle2 = mail(ingestor, frm=OLLE, subject="Brandväggen igen", body="Olle undrar igen", thread="olle2", days_ago=1)
    _arrive(conn)
    res2, _ = _new(conn)
    assert res2["accept"]["decided_by"] == backfill.policy_label({**backfill.TWO_LEVEL, "type": 0.95}, 0.15)
    rows = _model(conn, olle2)
    assert rows["origin"]["status"] == "active" and rows["type"]["status"] == "proposed"  # 0.90 < 0.95
    # enrich.json's accept wins over the stored one
    olle3 = mail(ingestor, frm=OLLE, subject="Tredje", body="Olle undrar en tredje gång", thread="olle3", days_ago=1)
    _arrive(conn)
    res3, _ = _new(conn, accept_policy={"thresholds": {"origin": 0.99}, "margin": 0.15})
    assert res3["policy"]["from"] == "enrich.json" and _model(conn, olle3)["origin"]["status"] == "proposed"


def test_the_structure_plan_places_the_enriched_mail_again(conn, ingestor, keychain):
    _judged(conn, ingestor)
    structure.plan(conn)
    conn.commit()
    new = _invoice(ingestor)
    _arrive(conn)
    structure.after_sync(conn)  # the sync placed it first, before it had any value
    conn.commit()
    before = conn.execute("select target from structure_plan where message_id = %s", (new,)).fetchone()["target"]
    assert before == structure.TO_SORT
    res, _ = _new(conn)
    after = conn.execute("select target from structure_plan where message_id = %s", (new,)).fetchone()["target"]
    assert res["placed"] == {"gmail": 1} and after != structure.TO_SORT


# ---------------------------------------------------------------- the guards and the dry run

def test_the_largest_units_wait_for_the_next_run_over_max_cases_or_the_budget(conn, ingestor, keychain):
    _judged(conn, ingestor)
    for i in range(3):
        mail(ingestor, frm=f"Kund {i} <kund{i}@example.org>", subject=f"Fråga {i}", body=f"Olle {i}", thread=f"k{i}",
             days_ago=1)
    _arrive(conn)
    res, fake = _new(conn, max_cases=2)
    assert res["asked"]["cases"] == 2 and res["deferred"]["units"] == 1 and len(fake.requests) == 8
    res, fake = _new(conn, budget=0.0)
    assert res["asked"]["cases"] == 0 and res["deferred"]["units"] == 1 and fake.requests == []
    res, fake = _new(conn)
    assert res["asked"]["cases"] == 1 and res["deferred"]["units"] == 0


def test_a_dry_run_sends_nothing_reads_no_key_writes_nothing_and_explains(conn, ingestor, database, keychain):
    _judged(conn, ingestor)
    mail(ingestor, frm=OLLE, subject="Brandväggen", body="Olle undrar", thread="olle", days_ago=1)
    _invoice(ingestor)
    _arrive(conn)
    keychain.clear()
    runs = conn.execute("select count(*) as n from model_run").fetchone()["n"]
    from talos import db
    with db.connect(database, autocommit=True) as ro:
        res = incremental.run(ro, dry_run=True)
    assert keychain == [] and res["dry_run"]
    assert conn.execute("select count(*) as n from model_run").fetchone()["n"] == runs
    assert conn.execute("select count(*) as n from enrich_tick").fetchone()["n"] == 0
    assert res["reused"]["by_kind"] == {"template": 1} and res["asked"]["cases"] == 1
    assert res["estimate"]["cases"] == 1 and res["estimate"]["tokens_per_case"]["kind"] > 0
    text = incremental.format_dry_run(res)
    assert text.startswith("DRY RUN") and "Reused without Jev: 1 messages" in text and "Olle undrar" in text


# ---------------------------------------------------------------- the hook

def _on(home, **kw):
    (home / "enrich.json").write_text(json.dumps({"enabled": True, "daily_budget": 0.5, "every_minutes": 15, **kw}))


def test_the_hook_is_off_until_enrich_json_turns_it_on(conn, ingestor, tmp_path, database, keychain):
    _judged(conn, ingestor)
    mail(ingestor, frm=OLLE, subject="Brandväggen", body="Olle undrar", thread="olle", days_ago=1)
    _arrive(conn)
    fake = FakeJev(pick=_pick_all)
    assert incremental.after_sync(conn, tmp_path, database, client=client(fake)) is None
    assert fake.requests == [] and conn.execute("select count(*) as n from enrich_tick").fetchone()["n"] == 0
    assert incremental.settings(tmp_path) == incremental.DEFAULTS and not incremental.DEFAULTS["enabled"]
    _on(tmp_path)
    res = incremental.after_sync(conn, tmp_path, database, client=client(fake))
    assert res["asked"]["cases"] == 1 and len(fake.requests) == 4


def test_the_hook_runs_at_most_every_quarter_hour_and_within_its_daily_budget(conn, ingestor, tmp_path, database,
                                                                               keychain):
    _judged(conn, ingestor)
    _on(tmp_path)
    fake = FakeJev(pick=_pick_all, input_tokens=5000)
    mail(ingestor, frm=OLLE, subject="Brandväggen", body="Olle undrar", thread="olle", days_ago=1)
    _arrive(conn)
    t0 = datetime.now(timezone.utc)
    res = incremental.after_sync(conn, tmp_path, database, client=client(fake), now=t0)
    assert res["asked"]["cases"] == 1 and res["cost_usd"] == pytest.approx(jev.cost(4 * 5000))
    mail(ingestor, frm="Kund <kund@example.org>", subject="Fråga", body="Olle 2", thread="k", days_ago=1)
    _arrive(conn)
    assert incremental.after_sync(conn, tmp_path, database, client=client(fake), now=t0 + timedelta(minutes=5)) == {
        "skipped": "not due"}
    assert len(fake.requests) == 4
    assert incremental.spent_today(conn, t0) == pytest.approx(jev.cost(4 * 5000))
    # a day's budget of about one case, most of it spent: new threads wait, reuse (free) still happens
    _on(tmp_path, daily_budget=jev.cost(21_000))
    inv = _invoice(ingestor)
    _arrive(conn)
    res = incremental.after_sync(conn, tmp_path, database, client=client(fake), now=t0 + timedelta(minutes=16))
    assert res["deferred"]["units"] == 1 and res["asked"]["cases"] == 0 and len(fake.requests) == 4
    assert _model(conn, inv)["type"]["value"] == "invoice"
    # a new day, a new budget: it is asked
    conn.execute("update enrich_tick set started_at = started_at - interval '1 day'")
    conn.commit()
    res = incremental.after_sync(conn, tmp_path, database, client=client(fake), now=t0 + timedelta(minutes=32))
    assert res["asked"]["cases"] == 1 and len(fake.requests) == 8
    ticks = conn.execute("select trigger, ok from enrich_tick order by id").fetchall()
    assert [t["trigger"] for t in ticks] == ["sync"] * 3 and all(t["ok"] for t in ticks)


def test_the_hook_checks_in_with_argus_expecting_the_next_run_within_its_cadence(conn, ingestor, tmp_path, database,
                                                                              keychain):
    _judged(conn, ingestor)
    argus.seed(conn)
    _on(tmp_path)
    mail(ingestor, frm=OLLE, subject="Brandväggen", body="Olle undrar", thread="olle", days_ago=1)
    _arrive(conn)
    incremental.after_sync(conn, tmp_path, database, client=client(FakeJev(pick=_pick_all)))
    svc = conn.execute("select * from argus_service where slug = 'talos-enrich'").fetchone()
    assert svc["kind"] == "push" and svc["last_ok"] is True and "1 new" in svc["last_summary"]
    assert (svc["expected_next_at"] - svc["last_checkin_at"]).total_seconds() == 15 * 60 + argus.SYNC_EVERY


def test_the_hook_never_raises_and_reports_its_failure_to_argus(conn, ingestor, tmp_path, database, keychain,
                                                                monkeypatch):
    _judged(conn, ingestor)
    argus.seed(conn)
    _on(tmp_path)

    def boom(*a, **kw):
        raise RuntimeError("Jev is down")
    monkeypatch.setattr(incremental, "run", boom)
    assert incremental.after_sync(conn, tmp_path, database) is None
    svc = conn.execute("select last_ok, last_summary from argus_service where slug = 'talos-enrich'").fetchone()
    assert svc["last_ok"] is False and svc["last_summary"].startswith("failed: RuntimeError: Jev is down")
    (tmp_path / "enrich.json").write_text("{not json")
    assert incremental.after_sync(conn, tmp_path, database) is None
    # a failing run is stored as a failed tick, so the next one waits its turn
    monkeypatch.undo()
    _on(tmp_path)
    mail(ingestor, frm=OLLE, subject="Brandväggen", body="Olle undrar", thread="olle", days_ago=1)
    _arrive(conn)
    monkeypatch.setattr(incremental, "_run", boom)
    assert incremental.after_sync(conn, tmp_path, database) is None
    tick = conn.execute("select ok, summary from enrich_tick").fetchone()
    assert tick["ok"] is False and tick["summary"]["error"] == "RuntimeError: Jev is down"


def test_the_sync_runs_the_hook_after_its_steps_and_is_never_failed_by_it(conn, ingestor, tmp_path, database, keychain,
                                                                        monkeypatch, capsys):
    _judged(conn, ingestor)
    mail(ingestor, frm=OLLE, subject="Brandväggen", body="Olle undrar", thread="olle", days_ago=1)
    conn.commit()
    fake = FakeJev(pick=_pick_all)
    real = jev.JevClient
    monkeypatch.setattr(jev, "JevClient", lambda **kw: real(transport=fake.transport, sleep=_no_sleep, **kw))
    monkeypatch.setenv("TALOS_DSN", database)
    monkeypatch.setenv("TALOS_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_logging", lambda settings, verbose: None)
    _on(tmp_path)
    cli.main(["sync", "nobody", "--then-rules"])  # no account to fetch; the steps after it run
    assert "enrich new: 1 new" in capsys.readouterr().out and len(fake.requests) == 4

    def boom(*a, **kw):
        raise RuntimeError("broken")
    monkeypatch.setattr(incremental, "after_sync", boom)
    cli.main(["sync", "nobody", "--then-rules"])  # returns normally: no SystemExit


def test_the_command_line_dry_runs_runs_and_shows_the_status(conn, ingestor, tmp_path, database, keychain,
                                                            monkeypatch, capsys):
    _judged(conn, ingestor)
    mail(ingestor, frm=OLLE, subject="Brandväggen", body="Olle undrar", thread="olle", days_ago=1)
    _invoice(ingestor, days_ago=30)
    _arrive(conn)
    fake = FakeJev(pick=_pick_all)
    real = jev.JevClient
    monkeypatch.setattr(jev, "JevClient", lambda **kw: real(transport=fake.transport, sleep=_no_sleep, **kw))
    monkeypatch.setenv("TALOS_DSN", database)
    monkeypatch.setenv("TALOS_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_logging", lambda settings, verbose: None)
    cli.main(["enrich", "jev", "new", "--backlog", "--dry-run"])
    out = capsys.readouterr().out
    assert out.startswith("DRY RUN") and "Backlog: every unjudged message" in out and fake.requests == []
    assert "Reused without Jev: 1 messages" in out  # the invoice of 30 days ago, only with --backlog
    cli.main(["enrich", "jev", "new"])
    out = capsys.readouterr().out
    assert "\n1 new;" in out and "accepted (policy:" in out and len(fake.requests) == 4
    cli.main(["enrich", "jev", "new", "--status"])
    out = capsys.readouterr().out
    assert '"enabled": false' in out and "cli  ok" in out
