"""The mailbox structure planner (talos.structure): dry placement, never a mailbox write."""

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mailfactory as mf
import pytest
from starlette.testclient import TestClient

from talos import structure
from talos.config import Settings
from talos.ingest import Location
from talos.web import app

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
ME = "owner@gmail.com"
RULES = structure.load()
N = {"uid": 0}


def mail(ingestor, account="gmail", *, frm="Oskar Nyström <oskar@nordvik.se>", subject="Hej", days=30,
         labels=("\\Inbox",), folder=None, flags=("seen",), thread=None, to=None, msgid=None, in_reply_to=None):
    N["uid"] += 1
    at = NOW - timedelta(days=days)
    folder = folder or ("[all]" if account == "gmail" else "Inkorgen")
    raw = mf.make(frm=frm, to=to or (ME if account == "gmail" else "owner@company.example"),
                  subject=f"{subject} {N['uid']}", date=at, msgid=msgid, in_reply_to=in_reply_to)
    return ingestor.ingest(account, raw, Location(folder, f"k{N['uid']}", uidvalidity=1, uid=N["uid"],
                                                  provider_id=f"p{N['uid']}", labels=list(labels) if account == "gmail" else [],
                                                  flags=list(flags), provider_thread_id=thread, received_at=at)).message_id


def val(conn, mid, dim, value, *, source="model", status="active"):
    conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, status)"
                 " values (%s, %s, %s, %s, %s)", (mid, dim, value, source, status))


def vals(conn, mid, **kv):
    for k, v in kv.items():
        val(conn, mid, k, v)


def placed(conn):
    return {r["message_id"]: r for r in conn.execute("select * from structure_plan")}


@pytest.fixture
def kind_dimension(conn):
    """The coarse kind (feat/kind) may or may not exist; this test database gets it for a while."""
    conn.execute("insert into dimension (id, label, cardinality) values ('kind', 'Kind', 'one') on conflict do nothing")
    conn.commit()
    yield
    conn.rollback()
    conn.execute("delete from assignment where dimension_id = 'kind'")
    conn.execute("delete from dimension where id = 'kind'")
    conn.commit()


# ---------------------------------------------------------------------------- the rules

def test_the_shipped_rules_load_and_every_label_is_under_talos():
    assert set(RULES.accounts) == {"gmail", "work"}
    for a in RULES.accounts.values():
        for t in a.targets:
            assert t.place != "label" or t.path.startswith("Talos/")
        assert a.targets[-1].path == structure.TO_SORT and a.targets[-1].when == []


def test_every_taxonomy_topic_has_an_area_in_both_accounts():
    topics = [v["value"] for v in json.loads((Path(__file__).parent.parent / "rules" / "taxonomy.json").read_text())
              ["dimensions"]["topic"]["values"]]
    for acc, a in RULES.accounts.items():
        exact, prefixes = set(), []
        for t in a.targets:
            if not t.id.startswith("area-"):
                continue
            for c in structure.expand(RULES, t.when):
                for x in c.get("any", [c]):
                    if x.get("field") == "topic":
                        exact.update(x.get("in", []))
                        prefixes += x.get("prefix", [])
        missing = [t for t in topics if t not in exact and not any(t.startswith(p) for p in prefixes)]
        assert missing == [], f"{acc}: topics with no area: {missing}"


def test_a_label_outside_the_talos_namespace_is_refused():
    raw = json.loads(Path(structure.DEFAULT_PATH).read_text())
    raw["accounts"]["gmail"]["targets"].insert(0, {"id": "bad", "place": "label", "path": "Receipts", "when": []})
    with pytest.raises(structure.StructureError, match="only under Talos/"):
        structure.parse(raw)


def test_an_unknown_field_or_operator_is_refused_on_load():
    raw = json.loads(Path(structure.DEFAULT_PATH).read_text())
    raw["accounts"]["gmail"]["targets"].insert(0, {"id": "bad", "place": "label", "path": "Talos/X",
                                                   "when": [{"field": "colour", "in": ["red"]}]})
    with pytest.raises(structure.StructureError, match="unknown field"):
        structure.parse(raw)
    raw["accounts"]["gmail"]["targets"][0]["when"] = [{"field": "topic", "in": ["A"], "set": True}]
    with pytest.raises(structure.StructureError, match="one operator"):
        structure.parse(raw)


# ---------------------------------------------------------------------------- matching and order

def test_the_first_matching_target_wins_in_the_documented_order(conn, ingestor):
    receipt = mail(ingestor, frm="Klarna <noreply@klarna.com>")      # a record and shopping: Keep beats Areas
    vals(conn, receipt, sender_kind="machine", keep="keep", value="record_financial", topic="Shopping", type="receipt")
    newsletter = mail(ingestor, frm="Nyhetsbrev <news@shop.example>")  # machine, short-lived: Automated beats Areas
    vals(conn, newsletter, sender_kind="machine", keep="short_lived", value="noise", topic="Shopping", type="newsletter")
    phish = mail(ingestor, days=2, frm="IT-support <help@m1crosoft.example>")  # spam beats the inbox
    vals(conn, phish, sender_kind="people", type="spam", value="noise")
    val(conn, phish, "ask", "action")
    kept_spam = mail(ingestor, frm="Bank <info@bank.example>")        # but a record is kept, even when spam-like
    vals(conn, kept_spam, sender_kind="machine", origin="spam", value="record_financial")
    school = mail(ingestor, frm="Skolan <info@skola.example>")         # a person, old, no ask: filed by topic
    vals(conn, school, sender_kind="people", keep="short_lived", value="context", topic="School")
    conn.commit()
    structure.plan(conn, RULES, account="gmail", now=NOW)
    p = placed(conn)
    assert p[receipt]["target"] == "Talos/Keep/Receipts & invoices"
    assert p[newsletter]["target"] == "Talos/Automated/Marketing & newsletters"
    assert p[phish]["target"] == "Talos/Automated/Spam candidates" and p[phish]["leaves_inbox"]
    assert p[kept_spam]["target"] == "Talos/Keep/Receipts & invoices"
    assert p[school]["target"] == "Talos/Areas/School" and p[school]["rule_id"] == "area-school-first"
    assert {r["rules_version"] for r in p.values()} == {RULES.version}


def test_work_machine_mail_goes_to_automated_by_type_not_to_its_area(conn, ingestor):
    backup = mail(ingestor, "work", frm="Veeam <noreply@backup.example>", folder="Backup")
    vals(conn, backup, sender_kind="machine", keep="short_lived", value="transient", topic="Work/Backup", type="backup")
    person = mail(ingestor, "work", frm="Erik Holm <erik.holm@company.example>", folder="Arkiv")
    vals(conn, person, sender_kind="people", keep="short_lived", value="context", topic="Work/Backup")
    by_origin = mail(ingestor, "work", frm="Monitor <alerts@monitor.example>", folder="Alarm")  # no type: the origin
    vals(conn, by_origin, sender_kind="machine", keep="short_lived", origin="alert")
    conn.commit()
    structure.plan(conn, RULES, account="work", now=NOW)
    p = placed(conn)
    assert p[backup]["target"] == "Talos/Automated/Alerts & reports"
    assert p[person]["target"] == "Talos/Areas/IT Operations"
    assert p[by_origin]["target"] == "Talos/Automated/Alerts & reports"


def test_kind_is_used_when_the_message_has_one_and_type_otherwise(conn, ingestor, kind_dimension):
    with_kind = mail(ingestor, frm="Service <noreply@service.example>")
    vals(conn, with_kind, sender_kind="machine", keep="short_lived", type="newsletter", kind="alert")
    without = mail(ingestor, frm="Service <noreply@service.example>")
    vals(conn, without, sender_kind="machine", keep="short_lived", type="newsletter")
    conn.commit()
    structure.plan(conn, RULES, account="gmail", now=NOW)
    p = placed(conn)
    assert p[with_kind]["target"] == "Talos/Automated/Alerts & reports"
    assert p[without]["target"] == "Talos/Automated/Marketing & newsletters"


def test_a_human_decision_beats_the_model_in_placement(conn, ingestor):
    mid = mail(ingestor, frm="Klarna <noreply@klarna.com>")
    vals(conn, mid, sender_kind="machine", keep="short_lived", value="noise", type="promotion")
    val(conn, mid, "value", "record_financial", source="human")
    conn.commit()
    structure.plan(conn, RULES, account="gmail", now=NOW)
    assert placed(conn)[mid]["target"] == "Talos/Keep/Receipts & invoices"


# ---------------------------------------------------------------------------- the inbox

def test_the_inbox_keeps_people_with_an_open_ask_and_recent_people_mail(conn, ingestor):
    recent = mail(ingestor, days=3)
    vals(conn, recent, sender_kind="people", topic="Family")
    old = mail(ingestor, days=30)
    vals(conn, old, sender_kind="people", topic="Family", keep="short_lived", value="context")
    asking = mail(ingestor, days=30, thread="t-ask")
    vals(conn, asking, sender_kind="people", topic="Family")
    val(conn, asking, "ask", "question", status="proposed")  # Jev's ask is only proposed: it still counts
    answered = mail(ingestor, days=30, thread="t-done", msgid="<q@test.invalid>")
    vals(conn, answered, sender_kind="people", topic="Family")
    val(conn, answered, "ask", "question")
    mail(ingestor, days=29, frm=f"Alex <{ME}>", to="oskar@nordvik.se", thread="t-done", labels=("\\Sent",),
         in_reply_to="<q@test.invalid>")
    archived = mail(ingestor, days=3, labels=())  # recent, a person, with an ask, but not in the inbox
    vals(conn, archived, sender_kind="people", topic="Family")
    val(conn, archived, "ask", "action")
    flagged = mail(ingestor, days=90, flags=("seen", "flagged"))
    vals(conn, flagged, sender_kind="machine", keep="short_lived", type="newsletter")
    conn.commit()
    structure.plan(conn, RULES, account="gmail", now=NOW)
    p = placed(conn)
    assert (p[recent]["place"], p[recent]["rule_id"]) == ("inbox", "inbox-recent")
    assert p[old]["target"] == "Talos/Areas/Family & home" and p[old]["leaves_inbox"]
    assert p[asking]["rule_id"] == "inbox-open-ask" and not p[asking]["leaves_inbox"]
    assert p[answered]["target"] == "Talos/Areas/Family & home" and p[answered]["leaves_inbox"]
    assert p[archived]["place"] == "label" and not p[archived]["in_inbox"] and not p[archived]["leaves_inbox"]
    assert p[flagged]["rule_id"] == "inbox-flagged"


def test_the_inbox_edge_is_fourteen_days(conn, ingestor):
    thirteen = mail(ingestor, days=13.9)
    fifteen = mail(ingestor, days=14.1)
    for m in (thirteen, fifteen):
        vals(conn, m, sender_kind="people", topic="Music")
    conn.commit()
    structure.plan(conn, RULES, account="gmail", now=NOW)
    p = placed(conn)
    assert p[thirteen]["place"] == "inbox" and p[fifteen]["target"] == "Talos/Areas/Music"


def test_exchange_inbox_is_the_inbox_folder_and_sent_items_are_left_alone(conn, ingestor):
    inbox = mail(ingestor, "work", days=2, frm="Erik Holm <erik.holm@company.example>")
    sent = mail(ingestor, "work", frm="Alex <o@company.example>", to="erik.holm@company.example", folder="Skickat")
    junk = mail(ingestor, "work", frm="Lottery <win@lotto.example>", folder="Skräppost")
    vals(conn, inbox, sender_kind="people")
    conn.commit()
    rep = structure.plan(conn, RULES, account="work", now=NOW)["work"]["report"]
    p = placed(conn)
    assert p[inbox]["place"] == "inbox" and p[inbox]["in_inbox"]
    assert p[sent]["place"] == "leave" and p[junk]["place"] == "leave"
    assert rep["inbox_before"] == 1 and rep["inbox_after"] == 1 and rep["left_alone"] == 2


# ---------------------------------------------------------------------------- To sort, Teams

def test_undecided_mail_goes_to_to_sort_unless_it_is_recent_in_the_inbox(conn, ingestor):
    old = mail(ingestor, days=40)
    fresh = mail(ingestor, days=1)
    person_no_topic = mail(ingestor, days=40)
    vals(conn, person_no_topic, sender_kind="people", value="context")
    conn.commit()
    rep = structure.plan(conn, RULES, account="gmail", now=NOW)["gmail"]["report"]
    p = placed(conn)
    assert p[old]["target"] == "Talos/To sort" and p[old]["leaves_inbox"]
    assert p[fresh]["place"] == "inbox"
    assert p[person_no_topic]["target"] == "Talos/To sort"
    assert rep["to_sort"] == 2 and rep["inbox_before"] == 3 and rep["inbox_after"] == 1


def test_teams_messages_are_never_placed(conn, ingestor):
    email = mail(ingestor)
    chat = mail(ingestor)
    conn.execute("update message set medium = 'teams_chat' where id = %s", (chat,))
    conn.execute("insert into account (id, provider, address) values ('teams', 'teams', 'o@company.example')")
    conn.commit()
    structure.plan(conn, RULES, now=NOW)
    assert set(placed(conn)) == {email}
    with pytest.raises(structure.StructureError, match="Teams"):
        structure.plan(conn, RULES, account="teams")
    assert structure.why(conn, chat, RULES)["placed"] is False


# ---------------------------------------------------------------------------- the plan table

def test_a_second_plan_replaces_the_first_and_keeps_changed_at_when_nothing_moved(conn, ingestor):
    a = mail(ingestor)
    vals(conn, a, sender_kind="people", topic="Music")
    b = mail(ingestor)
    conn.commit()
    structure.plan(conn, RULES, account="gmail", now=NOW)
    first = placed(conn)
    conn.execute("delete from message where id = %s", (b,))
    val(conn, a, "value", "memory", source="human")
    c = mail(ingestor)
    conn.commit()
    structure.plan(conn, RULES, account="gmail", now=NOW)
    second = placed(conn)
    assert set(second) == {a, c}
    assert second[a]["target"] == "Talos/Keep/Memories" and second[a]["changed_at"] > first[a]["changed_at"]
    conn.commit()
    structure.plan(conn, RULES, account="gmail", now=NOW)
    assert placed(conn)[a]["changed_at"] == second[a]["changed_at"]


def test_planning_twenty_thousand_messages_is_fast(conn, ingestor):
    """A synthetic archive, written straight into the tables: the plan is one INSERT … SELECT."""
    n = 20000
    conn.execute("""
        with e as (insert into entity (kind) select 'message' from generate_series(1, %(n)s) returning id),
        m as (insert into message (id, account_id, provider_key, direction, from_address, subject, received_at, parser_version)
              select id, 'gmail', 'syn' || id, 'in', 'x' || (id %% 97) || '@example.com', 'Syn ' || id,
                     %(now)s::timestamptz - (id %% 400) * interval '1 day', 1 from e returning id)
        insert into message_location (message_id, account_id, folder, uidvalidity, uid, labels, flags)
        select id, 'gmail', '[all]', 1, id, case when id %% 2 = 0 then array['\\Inbox'] else array['Family'] end, '{seen}' from m""",
                 {"n": n, "now": NOW})
    for dim, values in (("sender_kind", ["people", "machine", "machine"]), ("keep", ["short_lived", "keep", "short_lived"]),
                        ("value", ["noise", "record_financial", "context", "memory"]), ("topic", ["Music", "School", "Shopping", "Work/AI"]),
                        ("type", ["newsletter", "backup", "receipt", "promotion", "notification"])):
        conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, status)"
                     " select id, %s, (%s::text[])[1 + id %% %s], 'model', 'active' from message where provider_key like 'syn%%'"
                     " and id %% 7 <> 0", (dim, values, len(values)))
    conn.execute("analyze message; analyze assignment; analyze message_location")
    conn.commit()
    t0 = time.monotonic()
    res = structure.plan(conn, RULES, account="gmail", now=NOW)["gmail"]
    took = time.monotonic() - t0
    print(f"\nplanned {res['placed']} messages in {res['seconds']} s ({took:.2f} s with the report)")
    assert res["placed"] == n and res["report"]["total"] == n
    assert took < 15
    assert conn.execute("select count(*) as n from structure_plan").fetchone()["n"] == n


def test_the_report_counts_old_labels_that_map_to_the_same_place(conn, ingestor):
    same = mail(ingestor, labels=("\\Inbox", "Receipts"))
    vals(conn, same, sender_kind="machine", value="record_financial")
    other = mail(ingestor, labels=("Receipts",))
    vals(conn, other, sender_kind="machine", keep="short_lived", type="promotion")
    conn.commit()
    rep = structure.plan(conn, RULES, account="gmail", now=NOW)["gmail"]["report"]
    receipts = next(o for o in rep["old"] if o["old"] == "Receipts")
    assert receipts["count"] == 2 and receipts["same"] == 1 and receipts["maps_to"] == "Talos/Keep/Receipts & invoices"
    assert rep["same_place"] == 1
    assert "Receipts" in structure.format_report({"gmail": rep})


def test_preview_gives_the_same_report_and_stores_nothing(conn, ingestor):
    a = mail(ingestor)
    vals(conn, a, sender_kind="people", topic="Music")
    mail(ingestor, days=2)
    conn.commit()
    rep = structure.preview(conn, RULES, account="gmail", now=NOW)["gmail"]
    assert conn.execute("select count(*) as n from structure_plan").fetchone()["n"] == 0
    stored = structure.plan(conn, RULES, account="gmail", now=NOW)["gmail"]["report"]
    assert {k: rep[k] for k in ("total", "inbox_before", "inbox_after", "to_sort")} == \
           {k: stored[k] for k in ("total", "inbox_before", "inbox_after", "to_sort")}


# ---------------------------------------------------------------------------- changesets

def _gmail_plan(conn, ingestor):
    ids = {}
    for i in range(3):
        ids[f"r{i}"] = mail(ingestor, frm="Klarna <noreply@klarna.com>")
        vals(conn, ids[f"r{i}"], sender_kind="machine", value="record_financial")
    ids["news"] = mail(ingestor, labels=())
    vals(conn, ids["news"], sender_kind="machine", keep="short_lived", type="newsletter")
    ids["done"] = mail(ingestor, labels=("\\Inbox", "Talos/Keep/Receipts & invoices"))  # already labelled
    vals(conn, ids["done"], sender_kind="machine", value="record_financial")
    ids["stay"] = mail(ingestor, days=1)
    vals(conn, ids["stay"], sender_kind="people")
    conn.commit()
    structure.plan(conn, RULES, account="gmail", now=NOW)
    return ids


def test_gmail_changesets_are_one_add_label_per_target_and_labels_only_under_talos(conn, ingestor):
    ids = _gmail_plan(conn, ingestor)
    made = structure.changesets(conn, "gmail", rules=RULES)
    sets = [c for c in made if "id" in c]
    assert [(c["op"], c["args"].get("label"), c["will_change"]) for c in sets] == [
        ("add_label", "Talos/Keep/Receipts & invoices", 3), ("add_label", "Talos/Automated/Marketing & newsletters", 1),
        ("archive", None, 1)]
    assert sets[0]["title"] == "Structure: add Talos/Keep/Receipts & invoices, 3 messages"
    ops = conn.execute("select message_id, op, args, status from changeset_op order by message_id").fetchall()
    assert all(o["args"]["label"].startswith("Talos/") for o in ops if o["op"] == "add_label")
    labelled = {o["message_id"] for o in ops if o["op"] == "add_label"}
    assert ids["done"] not in labelled and ids["stay"] not in {o["message_id"] for o in ops}
    # The archive takes only what carries its label on the server already ("done"); the other three
    # wait for their labels: nothing leaves the inbox unlabelled.
    archive = conn.execute("select message_id from changeset_op where op = 'archive'").fetchall()
    assert [r["message_id"] for r in archive] == [ids["done"]]
    assert made[-1]["note"].startswith("3 messages will leave the inbox")
    # Running it again adds nothing: those messages are in an open changeset already.
    again = structure.changesets(conn, "gmail", rules=RULES)
    assert not [c for c in again if "id" in c]


def test_the_archive_changeset_takes_labelled_inbox_mail_and_can_be_asked_for_all(conn, ingestor):
    ids = _gmail_plan(conn, ingestor)
    made = structure.changesets(conn, "gmail", target="Talos/Keep/Receipts & invoices", rules=RULES)
    archive = [c for c in made if c.get("op") == "archive"]
    assert len(archive) == 1 and archive[0]["will_change"] == 1  # the one that carries its label already
    assert archive[0]["title"] == "Structure: archive 1 message out of the inbox (Talos/Keep/Receipts & invoices)"
    op = conn.execute("select message_id, inverse from changeset_op where changeset_id = %s", (archive[0]["id"],)).fetchone()
    assert op["message_id"] == ids["done"] and op["inverse"] == {"op": "add_label", "args": {"label": "\\Inbox"}}
    everything = structure.changesets(conn, "gmail", archive_unlabelled=True, rules=RULES)
    arch = [c for c in everything if c.get("op") == "archive"]
    assert arch and arch[0]["will_change"] == 3  # r0-r2; "done" is already in the open archive changeset


def test_a_limit_makes_small_changesets_for_the_first_steps(conn, ingestor):
    _gmail_plan(conn, ingestor)
    made = structure.changesets(conn, "gmail", target="Talos/Keep/Receipts & invoices", limit=1, rules=RULES)
    assert made[0]["will_change"] == 1 and made[0]["title"].endswith(", 1 message")


def test_structure_changesets_are_never_committed(conn, ingestor):
    _gmail_plan(conn, ingestor)
    structure.changesets(conn, "gmail", archive_unlabelled=True, rules=RULES)
    rows = conn.execute("select status, committed_at, selection from changeset").fetchall()
    assert rows and {r["status"] for r in rows} == {"planned"} and all(r["committed_at"] is None for r in rows)
    assert all("structure" in r["selection"] for r in rows)
    # And the module has no way to: it never calls commit or apply, nor builds an executor.
    code = re.sub(r'"""(.|\n)*?"""', '""', Path(structure.__file__).read_text())
    assert not re.search(r"\bcs\.(commit|apply)\(|changesets\.(commit|apply)\(|executors_for|from talos\.writeback|import writeback", code)


def test_exchange_gets_a_plan_and_no_changesets(conn, ingestor):
    m = mail(ingestor, "work")
    vals(conn, m, sender_kind="machine", value="record_financial")
    conn.commit()
    structure.plan(conn, RULES, account="work", now=NOW)
    with pytest.raises(structure.StructureError, match="planned only"):
        structure.changesets(conn, "work", rules=RULES)
    assert conn.execute("select count(*) as n from changeset").fetchone()["n"] == 0


def test_new_since_prepares_only_what_is_new(conn, ingestor):
    _gmail_plan(conn, ingestor)
    conn.execute("update structure_plan set changed_at = now() - interval '3 days'")
    conn.execute("update message set ingested_at = now() - interval '3 days'")
    fresh = mail(ingestor, frm="Klarna <noreply@klarna.com>")
    vals(conn, fresh, sender_kind="machine", value="record_financial")
    conn.commit()
    structure.refresh(conn, RULES, account="gmail", now=NOW)
    made = structure.changesets(conn, "gmail", new_since=structure.parse_since("1d"), rules=RULES)
    assert [(c["args"]["label"], c["will_change"]) for c in made if "id" in c] == [("Talos/Keep/Receipts & invoices", 1)]


# ---------------------------------------------------------------------------- after a sync

def test_after_a_sync_new_mail_is_placed_and_the_inbox_is_looked_at_again(conn, ingestor):
    aging = mail(ingestor, days=10)
    vals(conn, aging, sender_kind="people", topic="Music")
    conn.commit()
    assert structure.after_sync(conn, now=NOW) is None  # no plan yet: nothing to keep up
    structure.plan(conn, RULES, account="gmail", now=NOW)
    assert placed(conn)[aging]["place"] == "inbox"
    new = mail(ingestor, days=0.1)
    conn.commit()
    res = structure.after_sync(conn, now=NOW + timedelta(days=5))
    assert res["gmail"]["mode"] == "incremental" and res["gmail"]["placed"] == 2
    p = placed(conn)
    assert p[new]["place"] == "inbox" and p[aging]["target"] == "Talos/Areas/Music" and p[aging]["leaves_inbox"]


def test_the_post_sync_hook_never_raises(conn, ingestor, monkeypatch):
    mail(ingestor)
    conn.commit()
    structure.plan(conn, RULES, account="gmail", now=NOW)

    def boom(*a, **k):
        raise RuntimeError("the planner broke")

    monkeypatch.setattr(structure, "refresh", boom)
    assert structure.after_sync(conn) is None
    monkeypatch.setattr(structure, "load", lambda *a, **k: (_ for _ in ()).throw(structure.StructureError("bad file")))
    assert structure.after_sync(conn) is None
    assert conn.execute("select 1 as ok").fetchone()["ok"] == 1  # the connection is still usable


def test_the_sync_command_runs_the_hook_after_importance():
    src = (Path(structure.__file__).parent / "cli.py").read_text()
    assert src.index("importance.compute_recent(conn, days=30)") < src.index("structure.after_sync(conn)")


# ---------------------------------------------------------------------------- why, and the page

def test_why_shows_the_values_and_every_rule_tested_in_order(conn, ingestor):
    mid = mail(ingestor)
    vals(conn, mid, sender_kind="machine", keep="short_lived", type="backup", topic="Work/Backup")
    conn.commit()
    structure.plan(conn, RULES, account="gmail", now=NOW)
    w = structure.why(conn, mid, RULES, now=NOW)
    assert w["now"]["target"] == "Talos/Automated/Alerts & reports" and not w["stale"]
    assert w["features"]["sender"] == "machine" and w["features"]["type"] == "backup"
    first = next(t for t in w["tests"] if t["first"])
    assert first["id"] == "auto-alerts" and all(c["ok"] for c in first["conditions"])
    ask = next(t for t in w["tests"] if t["id"] == "inbox-open-ask")
    assert not ask["matched"] and any(not c["ok"] and "sender" in c["text"] for c in ask["conditions"])
    assert {v["dimension_id"] for v in w["values"]} >= {"sender_kind", "type"}


def test_the_structure_page_endpoints(conn, ingestor, vault, database):
    ids = _gmail_plan(conn, ingestor)
    structure.changesets(conn, "gmail", rules=RULES)
    conn.commit()
    c = TestClient(app.create(Settings(home=vault.root.parent, dsn=database), allowed_hosts=["testserver"]))
    d = c.get("/api/structure").json()
    g = next(a for a in d["accounts"] if a["account"] == "gmail")
    assert g["planned"] and g["inbox_before"] == 5 and g["inbox_after"] == 1 and d["rules"]["version"] == RULES.version
    talos = next(n for n in g["tree"] if n["name"] == "Talos")
    keep = next(n for n in talos["children"] if n["name"] == "Keep")
    receipts = next(n for n in keep["children"] if n["name"] == "Receipts & invoices")
    assert receipts["count"] == 4 and receipts["rules"][0]["id"] == "keep-receipts" and keep["count"] >= 4
    assert next(a for a in d["accounts"] if a["account"] == "work")["planned"] is False
    assert len(d["changesets"]) == 3 and all(x["status"] == "planned" for x in d["changesets"])
    # a dry run's detail (megabytes for a large changeset) stays on the Changesets page, not on this one
    import json
    cs = d["changesets"][0]["id"]
    conn.execute("update changeset set summary = coalesce(summary, '{}'::jsonb) || jsonb_build_object('dry_run', %s::jsonb) where id = %s",
                 (json.dumps({"ok": 2, "items": [{"id": i} for i in range(500)]}), cs))
    conn.commit()
    row = next(x for x in c.get("/api/structure").json()["changesets"] if x["id"] == cs)
    assert "dry_run" not in row["summary"] and row["summary"]
    ex = c.get("/api/structure/examples", params={"account": "gmail", "target": "Talos/Keep"}).json()
    assert ex["total"] == 4 and {r["target"] for r in ex["rows"]} == {"Talos/Keep/Receipts & invoices"}
    assert c.get("/api/structure/examples", params={"account": "gmail"}).status_code == 400
    w = c.get(f"/api/structure/why/{ids['stay']}").json()
    assert w["now"]["place"] == "inbox"
    assert c.get("/api/structure/why/999999").status_code == 404


# ---------------------------------------------------------------------------- how to make it real

def test_the_checklist_shows_each_step_with_its_live_state(conn, ingestor, vault, database):
    _gmail_plan(conn, ingestor)
    conn.commit()
    c = TestClient(app.create(Settings(home=vault.root.parent, dsn=database), allowed_hosts=["testserver"]))
    accs = {a["account"]: a for a in c.get("/api/structure/checklist").json()["accounts"]}
    g = accs["gmail"]
    steps = {s["id"]: s for s in g["steps"]}
    assert [s["id"] for s in g["steps"]] == ["plan", "labels", "archive", "old_rules", "retire"]
    assert steps["plan"]["done"] and steps["plan"]["count"] == 6 and steps["plan"]["rules_version"] == RULES.version
    receipts = next(t for t in steps["labels"]["targets"] if t["target"] == "Talos/Keep/Receipts & invoices")
    assert (receipts["planned"], receipts["on_server"], receipts["waiting"], receipts["to_prepare"]) == (4, 1, 0, 3)
    assert steps["labels"]["done"] is False and steps["labels"]["on_server"] == 1
    a = steps["archive"]
    assert (a["planned"], a["in_inbox"], a["ready"], a["waiting"], a["to_prepare"], a["left"]) == (4, 4, 1, 0, 1, 0)
    assert steps["old_rules"]["manual"] and steps["retire"]["manual"]
    assert g["can_prepare"] and g["next_size"] == 1       # write-back starts with one message per label
    work = accs["work"]
    assert [s["id"] for s in work["steps"]] == ["plan", "consent", "old_rules", "retire"]
    assert work["steps"][0]["done"] is False and work["steps"][1]["done"] is False and not work["can_prepare"]
    conn.execute("""update account set settings = settings || '{"writeback_enabled": true}' where id = 'work'""")
    conn.commit()
    work = next(x for x in c.get("/api/structure/checklist").json()["accounts"] if x["account"] == "work")
    assert work["steps"][1]["done"] is True


def test_the_prepare_button_makes_planned_changesets_only(conn, ingestor, vault, database):
    _gmail_plan(conn, ingestor)
    conn.commit()
    c = TestClient(app.create(Settings(home=vault.root.parent, dsn=database), allowed_hosts=["testserver"]))
    H = {"X-Talos": "1"}
    assert c.post("/api/structure/prepare", json={"account": "gmail", "limit": 1}).status_code == 403
    assert c.post("/api/structure/prepare", json={"account": "gmail", "limit": 0}, headers=H).status_code == 400
    r = c.post("/api/structure/prepare", json={"account": "work"}, headers=H)
    assert r.status_code == 400 and "planned only" in r.json()["error"]
    r = c.post("/api/structure/prepare", json={"account": "gmail", "limit": 1}, headers=H)
    assert r.status_code == 201
    made = r.json()["made"]
    assert [(m["op"], m["will_change"]) for m in made] == [("add_label", 1), ("add_label", 1), ("archive", 1)]
    rows = conn.execute("select status, committed_at from changeset").fetchall()
    assert {x["status"] for x in rows} == {"planned"} and all(x["committed_at"] is None for x in rows)
    g = next(x for x in c.get("/api/structure/checklist").json()["accounts"] if x["account"] == "gmail")
    steps = {s["id"]: s for s in g["steps"]}
    assert steps["labels"]["waiting"] == 2 and steps["labels"]["to_prepare"] == 2
    assert steps["archive"]["waiting"] == 1 and steps["archive"]["to_prepare"] == 0
    assert {x["id"] for x in g["waiting"]} == {m["id"] for m in made}
    # Once a structure changeset has been applied, the next ones grow: 1, then 10, 100, all.
    first = made[0]["id"]
    conn.execute("update changeset set status = 'done', finished_at = now() where id = %s", (first,))
    conn.execute("update changeset_op set status = 'done' where changeset_id = %s and status = 'pending'", (first,))
    conn.commit()
    assert structure.next_size(conn, "gmail") == 10
