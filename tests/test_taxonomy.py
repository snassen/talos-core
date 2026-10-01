"""talos taxonomy load: rules/taxonomy.json into the dimension table, refused while a dropped value is in use."""

import json

import pytest

from talos import cli, gold, personal, rules, taxonomy

pytestmark = pytest.mark.usefixtures("taxonomy_loaded")  # loaded, and put back afterwards

FILE = json.loads(taxonomy.DEFAULT_PATH.read_text(encoding="utf-8"))


def _dims(conn):
    return {r["id"]: r for r in conn.execute(
        "select id, label, cardinality, allowed, value_meta, description from dimension order by id")}


def _without(tmp_path, drop: dict[str, set[str]]):
    """A copy of the file without some values."""
    data = json.loads(json.dumps(FILE))
    for dim, values in drop.items():
        data["dimensions"][dim]["values"] = [v for v in data["dimensions"][dim]["values"] if v["value"] not in values]
    for d in data["dimensions"].values():  # a derived dimension's value map drops them too (kind lists types)
        src = d.get("derived_from") or {}
        if isinstance(src.get("values"), dict) and src.get("dimension") in drop:
            src["values"] = {k: [x for x in g if x not in drop[src["dimension"]]] for k, g in src["values"].items()}
    path = tmp_path / "taxonomy.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_the_load_sets_each_dimensions_values_in_file_order_with_their_words(conn):
    dims = _dims(conn)
    for dim, d in FILE["dimensions"].items():
        row = dims[dim]
        assert (row["label"], row["cardinality"], row["description"]) == (d["label"], d["cardinality"], d["description"])
        assert row["allowed"] == [v["value"] for v in d["values"]]
        assert row["value_meta"] == {v["value"]: {"family": v["family"], "label": v["label"],
                                                  "description": v["description"],
                                                  **({"common": v["common"]} if v.get("common") else {})}
                                     for v in d["values"]}
    assert (dims["ask"]["cardinality"], dims["value"]["cardinality"], dims["route"]["cardinality"]) == ("many", "one", "many")
    assert dims["tag"]["allowed"] is None and dims["tag"]["value_meta"] == {}  # a dimension not in the file is left be
    # the closed lists hold for rules too
    with pytest.raises(rules.RuleError):
        rules.save(conn, rules.Rule("x", "x", [{"field": "medium", "op": "is", "value": "email"}],
                                    {"dimension": "topic", "value": "Offert"}))


def test_a_second_load_writes_nothing(conn):
    before = _dims(conn)
    summary = taxonomy.load(conn)
    assert not any(s["changed"] for s in summary.values())
    assert _dims(conn) == before
    assert "0 of 11 dimensions written" in taxonomy.format_summary(summary)


def test_a_load_is_refused_whole_while_a_dropped_value_is_in_use(conn, ingestor, tmp_path):
    from test_enrich import mail
    mid = mail(ingestor, frm="Oskar <oskar@nordvik.se>", subject="Offert")
    rules.assign(conn, [mid], "topic", "Work/Customers")                       # an active assignment
    rules.save(conn, rules.Rule("rr", "Receipts", [{"field": "subject", "op": "starts_with", "value": "Kvitto"}],
                                {"dimension": "type", "value": "receipt"}, enabled=False))  # a rule, even switched off
    conn.execute("insert into gold_set (name, seed, target) values ('k', 1, 1)")
    conn.execute("insert into gold_item (set_id, position, message_id, stratum, reason, unit)"
                 " values (1, 1, %s, 'person', 'r', 'message')", (mid,))
    conn.execute("insert into gold_label (set_id, item_id, field, values, labeller) values (1, 1, 'value', '{memory}', %s),"
                 " (1, 1, 'route', '{4711}', %s)", (personal.OWNER_ID,) * 2)     # an answer; a route object id
    conn.commit()
    before = _dims(conn)
    path = _without(tmp_path, {"topic": {"Work/Customers", "Music"}, "type": {"receipt"}, "value": {"memory"},
                               "origin": {"spam"}})
    with pytest.raises(taxonomy.TaxonomyError) as exc:
        taxonomy.load(conn, path)
    msg = str(exc.value)
    assert "topic: Work/Customers (assignments 1)" in msg and "type: receipt (rule rr)" in msg
    assert "value: memory (answer key 1)" in msg
    assert "Music" not in msg and "spam" not in msg and "4711" not in msg  # unused ones may go; an object id is no value
    conn.rollback()
    assert _dims(conn) == before  # nothing was written
    # once nothing uses them, the same file loads, and the dropped values are gone
    conn.execute("delete from assignment where dimension_id = 'topic'")
    conn.execute("delete from rule")
    conn.execute("delete from gold_label where field = 'value'")
    summary = taxonomy.load(conn, path)
    assert set(summary["topic"]["removed"]) == {"Music", "Work/Customers"} and summary["origin"]["removed"] == ["spam"]
    assert "Music" not in _dims(conn)["topic"]["allowed"] and "Music" not in _dims(conn)["topic"]["value_meta"]
    # the answer key reads the object-id route label as unset
    assert gold.gold_labels(conn, 1) == {}


def test_a_broken_file_is_refused_before_anything_is_read_from_the_database(conn, tmp_path):
    bad = json.loads(json.dumps(FILE))
    bad["dimensions"]["ask"]["values"].append(dict(bad["dimensions"]["ask"]["values"][0]))  # listed twice
    (tmp_path / "twice.json").write_text(json.dumps(bad), encoding="utf-8")
    bad2 = json.loads(json.dumps(FILE))
    del bad2["dimensions"]["value"]["values"][0]["description"]
    (tmp_path / "words.json").write_text(json.dumps(bad2), encoding="utf-8")
    for name, words in (("twice.json", "listed twice"), ("words.json", "description"), ("nope.json", "cannot read")):
        with pytest.raises(taxonomy.TaxonomyError, match=words):
            taxonomy.load(conn, tmp_path / name)


def test_talos_taxonomy_load_and_talos_setup_load_the_file(conn, ingestor, database, vault, tmp_path, monkeypatch,
                                                           capsys):
    monkeypatch.setenv("TALOS_DSN", database)
    monkeypatch.setenv("TALOS_HOME", str(vault.root.parent))
    monkeypatch.setattr(cli, "_logging", lambda settings, verbose: None)
    cli.main(["taxonomy", "load"])
    assert "0 of 11 dimensions written" in capsys.readouterr().out
    path = _without(tmp_path, {"route": {"Side Quests"}})
    cli.main(["taxonomy", "load", str(path)])
    out = capsys.readouterr().out
    assert "1 of 11 dimensions written" in out and "route" in out and "(+0 −1)" in out
    cli.main(["setup"])  # the repo's file, loaded again: Side Quests is back
    out = capsys.readouterr().out
    assert "1 of 11 dimensions written" in out
    assert "Side Quests" in _dims(conn)["route"]["allowed"]
    # refused: a message, exit code 2, nothing written
    from test_enrich import mail
    rules.assign(conn, [mail(ingestor, subject="x")], "route", "Side Quests")
    conn.commit()
    with pytest.raises(SystemExit) as exc:
        cli.main(["taxonomy", "load", str(path)])
    assert exc.value.code == 2 and "route: Side Quests (assignments 1)" in capsys.readouterr().err
    monkeypatch.setattr(taxonomy, "DEFAULT_PATH", path)
    cli.main(["setup"])  # setup says why it did not load, and goes on
    out = capsys.readouterr().out
    assert "taxonomy not loaded: refused" in out and "Side Quests" in out and "data directory" in out
    monkeypatch.setattr(taxonomy, "DEFAULT_PATH", tmp_path / "missing.json")
    cli.main(["setup"])
    out = capsys.readouterr().out
    assert "taxonomy not loaded" in out and "is not there" in out
