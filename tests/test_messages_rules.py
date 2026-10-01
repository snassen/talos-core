"""The Messages sidebar and filters (senders, facets, top people) and rules edited in the UI."""

from datetime import datetime, timedelta, timezone

import bulkmail
import mailfactory as mf
from test_web import client

from talos import facets, rules, search
from talos.ingest import Location

POST = {"X-Talos": "1"}
NOW = datetime.now(timezone.utc)


def _mail(ingestor, frm, n, *, subject="Hej", thread=None, labels=(), automated=False, days_ago=1, account="gmail"):
    """n invented messages from one sender; returns their ids."""
    ids = []
    for i in range(n):
        key = f"{frm}-{subject}-{i}"
        raw = mf.make(frm=frm, subject=f"{subject} {i}", date=NOW - timedelta(days=days_ago, minutes=i),
                      msgid=f"<{abs(hash(key))}@test.invalid>",
                      headers={"Auto-Submitted": "auto-generated"} if automated else None)
        ids.append(ingestor.ingest(account, raw, Location("[all]", key, labels=list(labels), provider_thread_id=thread,
                                                          received_at=NOW - timedelta(days=days_ago, minutes=i)))
                   .message_id)
    return ids


def _archive(ingestor, conn):
    ids = {
        "oskar": _mail(ingestor, "Oskar Nyström <oskar@nordvik.se>", 3, subject="Offert", labels=["Kunder"]),
        "helena": _mail(ingestor, "Helena Roos <helena.roos@nordvik.se>", 1, subject="Möte", labels=["Kunder", "Viktigt"]),
        "klarna": _mail(ingestor, "Klarna <noreply@klarna.com>", 2, subject="Kvitto", labels=["Receipts"], automated=True),
        "anna": _mail(ingestor, "Anna Berg <anna.berg@company.example>", 1, subject="Lunch"),
    }
    conn.commit()
    return ids


# ---------------------------------------------------------------- senders

def test_the_sender_list_counts_the_matching_messages_most_first(conn, ingestor, vault, database):
    _archive(ingestor, conn)
    res = client(database, vault).get("/api/senders").json()
    assert res["total"] == 7 and res["senders"] == 4
    assert [(r["address"], r["n"]) for r in res["rows"][:2]] == [("oskar@nordvik.se", 3), ("noreply@klarna.com", 2)]
    oskar = res["rows"][0]
    assert oskar["name"] == "Oskar Nyström" and oskar["share"] == round(3 / 7, 4) and oskar["automated"] is False


def test_the_sender_list_follows_the_filters_including_the_domain(conn, ingestor, vault, database):
    _archive(ingestor, conn)
    c = client(database, vault)
    nordvik = c.get("/api/senders", params={"domain": "nordvik.se"}).json()
    assert nordvik["total"] == 4
    assert [(r["address"], r["n"], r["share"]) for r in nordvik["rows"]] == [
        ("oskar@nordvik.se", 3, 0.75), ("helena.roos@nordvik.se", 1, 0.25)]
    people = c.get("/api/senders", params={"automated": "false"}).json()
    assert "noreply@klarna.com" not in {r["address"] for r in people["rows"]} and people["total"] == 5
    labelled = c.get("/api/senders", params={"label": "Viktigt"}).json()
    assert [r["address"] for r in labelled["rows"]] == ["helena.roos@nordvik.se"]
    searched = c.get("/api/senders", params={"q": "offert"}).json()
    assert [r["address"] for r in searched["rows"]] == ["oskar@nordvik.se"]


def test_the_sender_list_shows_twenty_unless_all_are_asked_for(conn, ingestor, vault, database):
    for i in range(25):
        _mail(ingestor, f"Person {i} <p{i}@exempel.se>", 1 + (i == 0), subject=f"Ärende {i}")
    conn.commit()
    c = client(database, vault)
    top = c.get("/api/senders").json()
    assert len(top["rows"]) == 20 and top["senders"] == 25 and top["rows"][0]["address"] == "p0@exempel.se"
    assert len(c.get("/api/senders", params={"all": "1"}).json()["rows"]) == 25
    assert len(c.get("/api/senders", params={"limit": "0"}).json()["rows"]) == 25
    assert len(c.get("/api/senders", params={"limit": "5000"}).json()["rows"]) == 25  # capped, not refused


def test_the_sender_filter_keeps_one_senders_messages(conn, ingestor, vault, database):
    ids = _archive(ingestor, conn)
    c = client(database, vault)
    res = c.get("/api/messages", params={"sender": "Oskar@Nordvik.se"}).json()
    assert res["total"] == 3 and {r["id"] for r in res["rows"]} == set(ids["oskar"])
    assert c.get("/api/messages", params={"sender": "oskar@nordvik.se", "q": "möte"}).json()["total"] == 0
    assert c.get("/api/messages", params={"sender": "nobody@nowhere.se"}).json()["total"] == 0


def test_a_domain_filter_reads_its_own_index(conn):
    bulkmail.fill(conn, 3000)
    sql, params = facets.senders_sql(None, domain="exempel7.se")
    words = "\n".join(r["QUERY PLAN"] for r in conn.execute("explain (costs off) " + sql, params))
    assert "message_from_domain_idx" in words and "Seq Scan on message" not in words
    assert facets.senders(conn, domain="exempel7.se")["total"] == len([i for i in range(1, 3001)
                                                                       if i % 60 == 7 and i % 101])


# ---------------------------------------------------------------- facets and dimension filters

def _typed(ingestor, conn):
    """Newsletters and receipts by rule, one human correction, and a thread with a human topic."""
    ids = _archive(ingestor, conn)
    trip = _mail(ingestor, "Oskar Nyström <oskar@nordvik.se>", 3, subject="Resa", thread="trip")
    conn.commit()
    rules.save(conn, rules.Rule("nl", "Nordvik is a newsletter", [{"field": "from_domain", "op": "is", "value": "nordvik.se"}],
                                {"dimension": "type", "value": "newsletter"}))
    rules.save(conn, rules.Rule("rc", "Klarna is a receipt", [{"field": "from_domain", "op": "is", "value": "klarna.com"}],
                                {"dimension": "type", "value": "receipt"}))
    rules.save(conn, rules.Rule("tp", "Offers are Sales", [{"field": "subject", "op": "contains", "value": "Offert"}],
                                {"dimension": "topic", "value": "Sales"}))
    rules.run_all(conn)
    rules.assign(conn, [ids["helena"][0]], "type", "conversation")  # a human beats the rule
    thread = conn.execute("select thread_id from message where id = %s", (trip[0],)).fetchone()["thread_id"]
    rules.assign(conn, [thread], "topic", "Travel")  # on the thread: every message in it
    rules.assign(conn, [trip[2]], "topic", "Work")  # but this one message's own human value wins
    conn.commit()
    return ids, trip


def test_the_facets_count_type_topic_and_label_for_the_selection(conn, ingestor, vault, database):
    _typed(ingestor, conn)
    c = client(database, vault)
    got = c.get("/api/facets").json()
    as_dict = lambda rows: {r["value"]: r["n"] for r in rows}
    # nordvik.se: oskar 3 + trip 3 are newsletters, helena was corrected to conversation.
    assert as_dict(got["type"]) == {"newsletter": 6, "receipt": 2, "conversation": 1}
    assert as_dict(got["topic"]) == {"Sales": 3, "Travel": 2, "Work": 1}
    assert as_dict(got["label"]) == {"Kunder": 4, "Receipts": 2, "Viktigt": 1}
    narrowed = c.get("/api/facets", params={"domain": "nordvik.se", "automated": "false"}).json()
    assert as_dict(narrowed["type"]) == {"newsletter": 6, "conversation": 1}
    assert as_dict(narrowed["label"]) == {"Kunder": 4, "Viktigt": 1}


def test_a_facet_ignores_its_own_filter_but_follows_the_others(conn, ingestor, vault, database):
    _typed(ingestor, conn)
    got = client(database, vault).get("/api/facets", params=[("dim", "type:newsletter"), ("label", "Kunder")]).json()
    as_dict = lambda rows: {r["value"]: r["n"] for r in rows}
    assert as_dict(got["type"]) == {"newsletter": 3, "conversation": 1}  # label Kunder holds, type does not
    assert as_dict(got["topic"]) == {"Sales": 3}  # both hold
    assert as_dict(got["label"]) == {"Kunder": 3}  # type holds, label does not (helena is conversation)


def test_facets_count_what_the_effective_value_view_says(conn, ingestor):
    _typed(ingestor, conn)
    for dim in ("type", "topic"):
        view = {r["value"]: r["n"] for r in conn.execute(
            "select value, count(*) n from effective_message_assignment where dimension_id = %s group by 1", (dim,))}
        assert {r["value"]: r["n"] for r in facets.facet(conn, dim)} == view
    # A many-value dimension counts each value once per message, message and thread together.
    thread = conn.execute("select thread_id from message where subject = 'Resa 0'").fetchone()["thread_id"]
    one = conn.execute("select id from message where subject = 'Resa 1'").fetchone()["id"]
    rules.assign(conn, [thread], "tag", "trip")
    rules.assign(conn, [one], "tag", "trip")
    rules.assign(conn, [one], "tag", "paid")
    view = {r["value"]: r["n"] for r in conn.execute(
        "select value, count(*) n from effective_message_assignment where dimension_id = 'tag' group by 1")}
    assert {r["value"]: r["n"] for r in facets.facet(conn, "tag")} == view == {"trip": 3, "paid": 1}


def test_type_and_topic_filter_by_message_and_by_thread_values(conn, ingestor, vault, database):
    ids, trip = _typed(ingestor, conn)
    c = client(database, vault)
    got = lambda *dims: {r["id"] for r in c.get("/api/messages", params=[("dim", d) for d in dims]).json()["rows"]}
    assert got("type:newsletter") == set(ids["oskar"]) | set(trip)  # rule values on messages
    assert got("type:conversation") == set(ids["helena"])  # the human correction, not the rule
    assert got("topic:Travel") == set(trip[:2])  # set on the thread
    assert got("topic:Work") == {trip[2]}  # the message's own human value beats the thread's
    assert got("type:newsletter", "topic:Sales") == set(ids["oskar"])  # every pair must hold
    assert got("type:receipt", "topic:Sales") == set()
    assert search.messages(conn, dimension=[("type", "newsletter"), ("topic", "Travel")])["total"] == 2


# ---------------------------------------------------------------- top people on the Overview

def test_top_senders_leave_out_the_owners_own_addresses_and_automated_mail(conn, ingestor, vault, database):
    _archive(ingestor, conn)
    mine = _mail(ingestor, "Alex <o@company.example>", 5, subject="Anteckning")
    old = _mail(ingestor, "Erik Holm <erik.holm@company.example>", 4, subject="Gammalt", days_ago=400)
    # Even if the owner's alias was recorded as incoming before it was listed as theirs, it stays out.
    conn.execute("update message set direction = 'in' where id = any(%s)", (mine,))
    conn.commit()
    c = client(database, vault)
    people = c.get("/api/top-senders", params={"people_only": "1"}).json()
    addresses = [r["address"] for r in people]
    assert addresses[:2] == ["erik.holm@company.example", "oskar@nordvik.se"]
    assert "o@company.example" not in addresses and "noreply@klarna.com" not in addresses
    assert people[1]["name"] == "Oskar Nyström" and people[1]["domain"] == "nordvik.se" and people[1]["n"] == 3
    everyone = [r["address"] for r in c.get("/api/top-senders", params={"people_only": "0"}).json()]
    assert "noreply@klarna.com" in everyone and "o@company.example" not in everyone
    recent = [r["address"] for r in c.get("/api/top-senders", params={"days": "30"}).json()]
    assert "erik.holm@company.example" not in recent and recent[0] == "oskar@nordvik.se"
    assert len(c.get("/api/top-senders", params={"limit": "2"}).json()) == 2
    assert old  # the old mail exists; only the window left it out


# ---------------------------------------------------------------- rules edited in the UI

def _rules(c):
    return {r["id"]: r for r in c.get("/api/rules").json()}


def test_a_rule_saved_in_the_ui_gets_a_new_version_and_the_run_applies_it(conn, ingestor, vault, database):
    ids = _archive(ingestor, conn)
    c = client(database, vault)
    new = c.post("/api/rules", headers=POST, json={
        "new": True, "name": "Nordvik är kund", "description": "From the Kunder label",
        "conditions": [{"field": "from_domain", "op": "is", "value": "nordvik.se"}],
        "action": {"dimension": "topic", "value": "Customers"}, "priority": 40})
    assert new.status_code == 201 and new.json()["rule"]["id"] == "nordvik-ar-kund"
    assert new.json()["rule"]["version"] == 1 and new.json()["rule"]["description"] == "From the Kunder label"
    ran = c.post("/api/rules/run", headers=POST).json()
    assert ran["counts"] == {"nordvik-ar-kund": 4} and ran["assigned"] == 4
    assert _rules(c)["nordvik-ar-kund"]["hits"] == 4
    # Narrowing the condition is a new version; the run replaces the old version's values.
    edit = c.post("/api/rules", headers=POST, json={
        "id": "nordvik-ar-kund", "name": "Nordvik är kund", "description": "Only Oskar",
        "conditions": [{"field": "from_address", "op": "is", "value": "oskar@nordvik.se"}],
        "action": {"dimension": "topic", "value": "Customers"}, "priority": 40}).json()
    assert edit["rule"]["version"] == 2 and edit["new_version"] is True
    c.post("/api/rules/run", headers=POST)
    assert _rules(c)["nordvik-ar-kund"]["hits"] == 3
    refs = {r["source_ref"] for r in conn.execute("select source_ref from assignment where source_kind = 'rule'")}
    assert refs == {"rule:nordvik-ar-kund@2"}
    tagged = c.get("/api/messages", params={"dim": "topic:Customers"}).json()
    assert {r["id"] for r in tagged["rows"]} == set(ids["oskar"])
    # A new name or description alone is not a new version.
    renamed = c.post("/api/rules", headers=POST, json={**edit["rule"], "name": "Oskar är kund"}).json()
    assert renamed["rule"]["version"] == 2 and renamed["new_version"] is False


def test_a_disabled_rule_stops_assigning_at_the_next_run(conn, ingestor, vault, database):
    _archive(ingestor, conn)
    rules.save(conn, rules.Rule("rc", "Klarna is a receipt", [{"field": "from_domain", "op": "is", "value": "klarna.com"}],
                                {"dimension": "type", "value": "receipt"}))
    conn.commit()
    c = client(database, vault)
    c.post("/api/rules/run", headers=POST)
    assert _rules(c)["rc"]["hits"] == 2
    off = c.post("/api/rules/rc/enabled", headers=POST, json={"enabled": False})
    assert off.status_code == 200 and off.json()["enabled"] is False and off.json()["version"] == 1
    assert c.post("/api/rules/run", headers=POST).json()["counts"] == {}
    assert _rules(c)["rc"]["hits"] == 0 and _rules(c)["rc"]["enabled"] is False
    assert c.post("/api/rules/nope/enabled", headers=POST, json={"enabled": True}).status_code == 404
    assert c.post("/api/rules/rc/enabled", headers=POST, json={"enabled": "yes"}).status_code == 400


def test_an_invalid_rule_is_refused_with_a_message_not_a_server_error(conn, ingestor, vault, database):
    _archive(ingestor, conn)
    c = client(database, vault)
    good = {"name": "Ok", "conditions": [{"field": "subject", "op": "contains", "value": "x"}],
            "action": {"dimension": "type", "value": "newsletter"}}
    cases = {
        "unknown field": {**good, "conditions": [{"field": "colour", "op": "is", "value": "red"}]},
        "unknown operator": {**good, "conditions": [{"field": "subject", "op": "resembles", "value": "x"}]},
        "broken regular expression": {**good, "conditions": [{"field": "subject", "op": "matches", "value": "(unclosed"}]},
        "not a date": {**good, "conditions": [{"field": "received", "op": "after", "value": "last tuesday-ish"}]},
        "no conditions": {**good, "conditions": []},
        "conditions not a list": {**good, "conditions": {"field": "subject"}},
        "unknown dimension": {**good, "action": {"dimension": "mood", "value": "happy"}},
        "no value": {**good, "action": {"dimension": "type", "value": ""}},
        "unknown object": {**good, "action": {"object": 999999}},
        "no name": {**good, "name": "  "},
        "bad id": {**good, "id": "Has Spaces!"},
        "bad priority": {**good, "priority": "soon"},
    }
    for why, body in cases.items():
        r = c.post("/api/rules", headers=POST, json=body)
        assert r.status_code == 400 and r.json()["error"], why
    assert c.get("/api/rules").json() == []  # nothing half-saved
    assert c.post("/api/rules", headers=POST, json={**good, "new": True}).status_code == 201
    taken = c.post("/api/rules", headers=POST, json={**good, "new": True})
    assert taken.status_code == 400 and "exists" in taken.json()["error"]
    assert c.post("/api/rules", headers=POST, content=b"not json").status_code == 400
    bad_preview = c.post("/api/rules/preview", headers=POST,
                         json={"conditions": [{"field": "subject", "op": "matches", "value": "(unclosed"}]})
    assert bad_preview.status_code == 400 and "PostgreSQL" in bad_preview.json()["error"]


def test_rule_writes_without_the_talos_header_are_refused(conn, ingestor, vault, database):
    _archive(ingestor, conn)
    rules.save(conn, rules.Rule("rc", "Klarna", [{"field": "from_domain", "op": "is", "value": "klarna.com"}],
                                {"dimension": "type", "value": "receipt"}))
    conn.commit()
    c = client(database, vault)
    body = {"new": True, "name": "X", "conditions": [{"field": "subject", "op": "contains", "value": "x"}],
            "action": {"dimension": "type", "value": "newsletter"}}
    assert c.post("/api/rules", json=body).status_code == 403
    assert c.post("/api/rules/run").status_code == 403
    assert c.post("/api/rules/rc/enabled", json={"enabled": False}).status_code == 403
    assert c.post("/api/rules/run", headers={"X-Talos": "0"}).status_code == 403
    assert [r["id"] for r in c.get("/api/rules").json()] == ["rc"] and _rules(c)["rc"]["enabled"] is True
    assert conn.execute("select count(*) n from assignment").fetchone()["n"] == 0  # nothing ran


def test_the_rule_editor_learns_the_dimensions_and_their_values(conn, ingestor, vault, database):
    _typed(ingestor, conn)
    dims = {d["id"]: d for d in client(database, vault).get("/api/dimensions").json()}
    assert {"type", "topic", "tag"} <= set(dims) and dims["tag"]["cardinality"] == "many"
    assert set(dims["type"]["values"]) == {"newsletter", "receipt", "conversation"}
