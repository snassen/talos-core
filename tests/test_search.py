import bulkmail
import mailfactory as mf

from talos import export, rules, search
from talos.ingest import Location


def plan(conn, q=None, **filters) -> str:
    sql, params = search.messages_sql(q, **filters)
    return "\n".join(r["QUERY PLAN"] for r in conn.execute("explain (costs off) " + sql, params))


def test_a_search_reads_its_indexes_not_the_whole_archive(conn):
    bulkmail.fill(conn, 3000)
    words = plan(conn, "fakturor")
    assert "message_text_search_idx" in words  # full text, both dictionaries and the prefix form
    assert "message_subject_trgm_idx" in words and "message_from_trgm_idx" in words  # literal fragments
    assert "Seq Scan on message_text" not in words
    assert "message_from_trgm_idx" in plan(conn, "nordvik", account="gmail")
    # The results are what they were: stems, prefixes, sender fragments, and a total over all pages.
    assert search.messages(conn, "fakturor", limit=5)["total"] == 3000 // 97
    assert search.messages(conn, "kvitton")["total"] == len([i for i in range(1, 3001) if i % 89 == 0 and i % 97])
    assert search.messages(conn, "nordvik")["total"] == 3000 // 101


def test_a_dimension_filter_starts_from_the_value_index(conn):
    bulkmail.fill(conn, 3000)
    text = plan(conn, dimension=("topic", "t7"))
    assert "assignment_value_idx" in text and "Seq Scan on assignment" not in text
    assert search.messages(conn, dimension=("topic", "t7"))["total"] == 3000 // 20
    in_human_threads = conn.execute("select count(*) n from message m join thread t on t.id = m.thread_id"
                                    " where t.id % 50 = 0").fetchone()["n"]
    assert search.messages(conn, dimension=("topic", "human-thread"))["total"] == in_human_threads > 0


def _thread_of_two(ingestor, conn):
    a = ingestor.ingest("gmail", mf.make(subject="Kvitto hemförsäkring", msgid="<a@nordvik.se>"),
                        Location("[all]", "a", provider_thread_id="t1")).message_id
    b = ingestor.ingest("gmail", mf.make(subject="SV: Kvitto hemförsäkring", msgid="<b@nordvik.se>"),
                        Location("[all]", "b", provider_thread_id="t1")).message_id
    other = ingestor.ingest("gmail", mf.make(subject="Middag"), Location("[all]", "c", provider_thread_id="t2")).message_id
    thread = conn.execute("select thread_id from message where id = %s", (a,)).fetchone()["thread_id"]
    return a, b, other, thread


def ids(res):
    return {r["id"] for r in res["rows"]}


def test_a_human_decision_on_a_thread_beats_a_rule_on_its_messages(conn, ingestor):
    a, b, other, thread = _thread_of_two(ingestor, conn)
    rules.save(conn, rules.Rule(id="kvitto", name="Kvitton", action={"dimension": "topic", "value": "Kvitto"},
                                conditions=[{"field": "subject", "op": "contains", "value": "kvitto"}]))
    rules.run_all(conn)
    rules.assign(conn, [thread], "topic", "Försäkring")
    conn.commit()
    assert ids(search.messages(conn, dimension=("topic", "Försäkring"))) == {a, b}
    assert ids(search.messages(conn, dimension=("topic", "Kvitto"))) == set()
    values = search.message(conn, a)["values"]
    assert [(v["value"], v["source_kind"], v["via"]) for v in values] == [("Försäkring", "human", "thread")]
    # The entity-level view is unchanged: the rule's value is still the message's own.
    own = conn.execute("select value from effective_assignment where entity_id = %s", (a,)).fetchone()["value"]
    assert own == "Kvitto"


def test_a_message_s_own_decision_beats_its_thread_s_in_the_same_tier(conn, ingestor):
    a, b, other, thread = _thread_of_two(ingestor, conn)
    rules.assign(conn, [thread], "topic", "Försäkring")
    rules.assign(conn, [b], "topic", "Privat")
    conn.commit()
    assert ids(search.messages(conn, dimension=("topic", "Försäkring"))) == {a}
    assert ids(search.messages(conn, dimension=("topic", "Privat"))) == {b}
    assert [(v["value"], v["via"]) for v in search.message(conn, b)["values"]] == [("Privat", "message")]


def test_a_many_value_dimension_counts_the_message_s_and_the_thread_s_values_once(conn, ingestor):
    a, b, other, thread = _thread_of_two(ingestor, conn)
    rules.assign(conn, [a], "tag", "hem")
    rules.assign(conn, [thread], "tag", "hem")
    rules.assign(conn, [thread], "tag", "försäkring")
    conn.commit()
    assert sorted((v["value"], v["via"]) for v in search.message(conn, a)["values"]) == \
        [("försäkring", "thread"), ("hem", "message")]
    assert ids(search.messages(conn, dimension=("tag", "hem"))) == {a, b}


def test_the_snapshot_exports_each_message_s_effective_values(conn, ingestor, tmp_path):
    import pyarrow.parquet as pq
    a, b, other, thread = _thread_of_two(ingestor, conn)
    rules.assign(conn, [thread], "topic", "Försäkring")
    conn.commit()
    rows = pq.read_table(export.snapshot(conn, tmp_path) / "message_values.parquet").to_pylist()
    assert sorted((r["message_id"], r["value"], r["via"]) for r in rows) == \
        [(a, "Försäkring", "thread"), (b, "Försäkring", "thread")]
