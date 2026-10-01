"""People / Automated in Messages go by origin (falling back to the automated flag), and Origin is a facet."""

from test_messages_rules import _mail
from test_web import client

import pytest

from talos import enrich, rules, search


def _origin(conn, entity_id, value, kind="rule", ref="prepass:origin.test@1"):
    if kind == "human":
        rules.assign(conn, [entity_id], "origin", value)
        return
    conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref)"
                 " values (%s, 'origin', %s, %s, %s)", (entity_id, value, kind, ref))


def _thread(conn, mid):
    return conn.execute("select thread_id from message where id = %s", (mid,)).fetchone()["thread_id"]


def _archive(ingestor, conn) -> dict[str, int]:
    """One message per way its origin can be decided; the value says People (False) or Automated (True)."""
    m = {
        "via_person": _mail(ingestor, "Anna Berg via Blocket <noreply@blocket.se>", 1, subject="Svar", automated=True)[0],
        "alarm": _mail(ingestor, "Larmbolaget <larm@larmbolaget.example>", 1, subject="Larm")[0],
        "flag_only": _mail(ingestor, "Tjänst <noreply@tjanst.example>", 1, subject="Kod", automated=True)[0],
        "plain": _mail(ingestor, "Oskar Nyström <oskar@nordvik.se>", 1, subject="Offert", thread="o1")[0],
        "thread_wins": _mail(ingestor, "Nyhetsbrev <brev@klubb.example>", 1, subject="Klubbnytt", thread="k1")[0],
        "own_disagree": _mail(ingestor, "Drift <drift@nordvik.se>", 1, subject="Server nere")[0],
        "rule_over_model": _mail(ingestor, "Lista <lista@example.org>", 1, subject="Veckans", automated=True, thread="l1")[0],
    }
    _origin(conn, m["via_person"], "person_via_system", "human")
    _origin(conn, m["alarm"], "alert")
    _origin(conn, m["thread_wins"], "person")                                   # a rule says person …
    _origin(conn, _thread(conn, m["thread_wins"]), "marketing", "human")        # … the owner's word on the thread wins
    _origin(conn, m["own_disagree"], "person")
    _origin(conn, m["own_disagree"], "notification", "human")
    _origin(conn, m["rule_over_model"], "list")                                 # people writing to a list
    _origin(conn, _thread(conn, m["rule_over_model"]), "person", "model", "jev-1")  # an accepted model value: weaker
    conn.commit()
    return m


EXPECTED = {"via_person": False, "alarm": True, "flag_only": True, "plain": False, "thread_wins": True,
            "own_disagree": True, "rule_over_model": False}


def test_machine_made_is_the_effective_origin_or_else_the_automated_flag(conn, ingestor):
    m = _archive(ingestor, conn)
    got = {name: conn.execute(f"select ({search.machine_sql()}) as machine from message m where m.id = %s",
                              (mid,)).fetchone()["machine"] for name, mid in m.items()}
    assert got == EXPECTED
    # the same as effective_message_assignment's ranking, message by message
    for mid in m.values():
        eff = conn.execute("select value from effective_message_assignment where message_id = %s"
                           " and dimension_id = 'origin'", (mid,)).fetchone()
        flag = conn.execute("select is_automated from message where id = %s", (mid,)).fetchone()["is_automated"]
        assert got[next(k for k, v in m.items() if v == mid)] == (
            eff["value"] not in enrich.PERSON_ORIGINS if eff else flag)


def test_people_and_automated_follow_origin_in_the_list_and_every_count(conn, ingestor, vault, database):
    m = _archive(ingestor, conn)
    c = client(database, vault)
    for flag, want in (("false", {k for k, v in EXPECTED.items() if not v}), ("true", {k for k, v in EXPECTED.items() if v})):
        ids = {r["id"] for r in c.get("/api/messages", params={"automated": flag}).json()["rows"]}
        assert ids == {m[k] for k in want}
        n = len(want)
        assert c.get("/api/senders", params={"automated": flag}).json()["total"] == n
        assert c.get("/api/domains", params={"automated": flag}).json()["total"] == n
        assert c.get("/api/threads", params={"automated": flag}).json()["total"] == n  # one thread each
        facets = c.get("/api/facets", params={"automated": flag}).json()
        assert sum(r["n"] for r in facets["account"]) == n
    # the sender bars say the same: Larmbolaget has no automated headers but its origin is an alert
    senders = {r["address"]: r["automated"] for r in c.get("/api/senders").json()["rows"]}
    assert senders["larm@larmbolaget.example"] is True and senders["noreply@blocket.se"] is False
    assert senders["oskar@nordvik.se"] is False and senders["noreply@tjanst.example"] is True


def test_origin_is_a_facet_and_a_filter_like_type_and_topic(conn, ingestor, vault, database):
    m = _archive(ingestor, conn)
    c = client(database, vault)
    counts = {r["value"]: r["n"] for r in c.get("/api/facets").json()["origin"]}
    assert counts == {"person_via_system": 1, "alert": 1, "marketing": 1, "notification": 1, "list": 1}
    # the facet ignores its own filter, and follows the others
    assert {r["value"] for r in c.get("/api/facets", params={"dim": "origin:alert"}).json()["origin"]} == set(counts)
    assert {r["value"]: r["n"] for r in c.get("/api/facets", params={"automated": "false"}).json()["origin"]} \
        == {"person_via_system": 1, "list": 1}
    res = c.get("/api/messages", params={"dim": "origin:marketing"}).json()
    assert {r["id"] for r in res["rows"]} == {m["thread_wins"]}  # the thread's value, which beats the message's
    assert c.get("/api/messages", params={"dim": "origin:person"}).json()["total"] == 0  # overruled everywhere


def test_list_counts_as_people_and_every_other_machine_origin_as_automated(conn, ingestor):
    assert set(enrich.PERSON_ORIGINS) == {"person", "person_via_system", "list"}
    assert set(enrich.MACHINE_ORIGINS) == {"transactional", "notification", "alert", "system_report", "mail_system",
                                           "auto_reply", "marketing", "spam"}
    ids = {o: _mail(ingestor, f"X <{o}@example.org>", 1, subject=o, automated=True)[0]
           for o in ("list", "mail_system", "system_report", "auto_reply", "spam")}  # all with automated headers
    for o, mid in ids.items():
        _origin(conn, mid, o)
    got = {o: conn.execute(f"select ({search.machine_sql()}) as machine from message m where m.id = %s",
                           (mid,)).fetchone()["machine"] for o, mid in ids.items()}
    assert got == {"list": False, "mail_system": True, "system_report": True, "auto_reply": True, "spam": True}


@pytest.mark.usefixtures("taxonomy_loaded")
def test_the_origin_type_and_topic_facets_carry_the_taxonomys_labels_and_families(conn, ingestor, vault, database):
    m = _archive(ingestor, conn)
    rules.assign(conn, [m["plain"]], "type", "conversation")
    rules.assign(conn, [m["plain"]], "topic", "Work/Customers")
    # a value in use that the taxonomy does not describe (written before the list was closed)
    conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind) values (%s, 'type', 'fax', 'human')",
                 (m["alarm"],))
    conn.commit()
    f = client(database, vault).get("/api/facets").json()
    origin = {r["value"]: r for r in f["origin"]}
    assert origin["list"]["label"] == "Mailing list or group" and origin["list"]["family"] == "People"
    assert origin["alert"]["family"] == "Machine" and origin["alert"]["order"] == enrich.ORIGINS.index("alert")
    types = {r["value"]: r for r in f["type"]}
    assert types["conversation"]["family"] == "Conversation" and types["conversation"]["label"]
    assert "label" not in types["fax"] and "family" not in types["fax"]  # the raw id, in the UI
    topic = f["topic"][0]
    assert (topic["value"], topic["family"], topic["label"]) == ("Work/Customers", "Work", "Customers")
