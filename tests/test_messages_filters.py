"""The Messages view's account chips and hidden senders: real filters, so every count agrees."""

from test_messages_rules import _archive, _mail
from test_web import client


def _accounts(ingestor, conn):
    """The usual archive in gmail, two messages in work and three Teams chats."""
    ids = _archive(ingestor, conn)
    ids["work"] = _mail(ingestor, "Pia Ek <pia.ek@company.example>", 2, subject="Budget", account="work")
    conn.execute("insert into account (id, provider, address) values ('teams', 'teams', 'teams@talos.invalid')")
    ids["teams"] = _mail(ingestor, "Pia Ek <pia.ek@company.example>", 3, subject="Chatt", account="teams")
    conn.execute("update message set medium = 'teams_chat' where id = any(%s)", (ids["teams"],))
    conn.commit()
    return ids


def _totals(c, params):
    """What the list, the senders, the domains and the facets each say about one selection."""
    listed = c.get("/api/messages", params=params).json()
    facets = c.get("/api/facets", params=params).json()
    return {"list": listed["total"], "ids": {r["id"] for r in listed["rows"]},
            "senders": c.get("/api/senders", params=params).json()["total"],
            "domains": c.get("/api/domains", params=params).json()["total"],
            "accounts": {r["value"]: r["n"] for r in facets["account"]},
            "labels": {r["value"]: r["n"] for r in facets["label"]}}


def test_excluding_an_account_leaves_it_out_of_the_list_and_every_count(conn, ingestor, vault, database):
    ids = _accounts(ingestor, conn)
    c = client(database, vault)
    got = _totals(c, [("exclude_accounts", "teams")])
    assert got["ids"] == {i for k, v in ids.items() if k != "teams" for i in v}
    assert got["list"] == got["senders"] == got["domains"] == 9
    # The account facet ignores its own filter: Teams still says what it would add.
    assert got["accounts"] == {"gmail": 7, "work": 2, "teams": 3, "local": 0}
    two = _totals(c, [("exclude_accounts", "teams"), ("exclude_accounts", "gmail")])
    assert two["ids"] == set(ids["work"]) and two["list"] == two["senders"] == two["domains"] == 2
    assert two["labels"] == {}  # gmail carried every label


def test_including_accounts_keeps_only_those(conn, ingestor, vault, database):
    ids = _accounts(ingestor, conn)
    c = client(database, vault)
    got = _totals(c, [("accounts", "work"), ("accounts", "teams")])
    assert got["ids"] == set(ids["work"]) | set(ids["teams"])
    assert got["list"] == got["senders"] == got["domains"] == 5
    assert got["accounts"] == {"gmail": 7, "work": 2, "teams": 3, "local": 0}
    # The account counts follow every other filter.
    labelled = _totals(c, [("accounts", "work"), ("label", "Kunder")])
    assert labelled["list"] == 0 and labelled["accounts"] == {"gmail": 4, "work": 0, "teams": 0, "local": 0}


def test_a_hidden_sender_leaves_the_list_the_counts_and_the_sender_ranking(conn, ingestor, vault, database):
    ids = _accounts(ingestor, conn)
    c = client(database, vault)
    params = [("exclude_senders", "Oskar@Nordvik.se"), ("exclude_accounts", "teams")]
    got = _totals(c, params)
    assert not got["ids"] & set(ids["oskar"]) and got["list"] == got["senders"] == got["domains"] == 6
    assert got["labels"] == {"Kunder": 1, "Viktigt": 1, "Receipts": 2}
    assert got["accounts"]["gmail"] == 4
    senders = c.get("/api/senders", params=params).json()
    assert [r["address"] for r in senders["rows"]][:2] == ["noreply@klarna.com", "pia.ek@company.example"]
    assert "oskar@nordvik.se" not in {r["address"] for r in senders["rows"]} and senders["senders"] == 4


def test_a_hidden_domain_leaves_the_list_the_counts_and_the_domain_ranking(conn, ingestor, vault, database):
    ids = _accounts(ingestor, conn)
    c = client(database, vault)
    params = [("exclude_domains", "Nordvik.se"), ("exclude_senders", "noreply@klarna.com")]
    got = _totals(c, params)
    assert got["ids"] == set(ids["anna"]) | set(ids["work"]) | set(ids["teams"])
    assert got["list"] == got["senders"] == got["domains"] == 6
    assert got["labels"] == {}
    domains = c.get("/api/domains", params=params).json()
    assert [(r["domain"], r["n"], r["addresses"]) for r in domains["rows"]] == [("company.example", 6, 2)]
    everything = c.get("/api/domains").json()
    assert [r["domain"] for r in everything["rows"]] == ["company.example", "nordvik.se", "klarna.com"]
    assert everything["total"] == 12 and everything["rows"][1]["share"] == round(4 / 12, 4)


def test_blank_exclusion_parameters_are_no_filter(conn, ingestor, vault, database):
    _accounts(ingestor, conn)
    c = client(database, vault)
    got = _totals(c, [("exclude_accounts", ""), ("exclude_senders", " "), ("accounts", "")])
    assert got["list"] == got["senders"] == got["domains"] == 12


def test_mail_between_the_owners_own_accounts_is_both_received_and_sent(conn, ingestor, vault, database):
    import mailfactory as mf
    from talos.ingest import Location
    mid = ingestor.ingest("work", mf.make(frm="Alex <owner@gmail.com>", subject="dfgh"),
                          Location("Inkorgen", "1")).message_id
    conn.commit()
    assert conn.execute("select direction from message where id = %s", (mid,)).fetchone()["direction"] == "self"
    c = client(database, vault)
    for d in ("in", "out"):
        assert mid in {r["id"] for r in c.get("/api/messages", params={"direction": d}).json()["rows"]}


def test_the_mail_direction_leaves_teams_both_ways_and_teams_has_a_direction_of_its_own(conn, ingestor, vault, database):
    ids = _accounts(ingestor, conn)
    mine = ids["teams"][0]
    conn.execute("update message set direction = 'out' where id = %s", (mine,))
    sent_mail = ids["work"][0]
    conn.execute("update message set direction = 'out' where id = %s", (sent_mail,))
    conn.commit()
    c = client(database, vault)
    listed = lambda params: {r["id"] for r in c.get("/api/messages", params=params).json()["rows"]}
    received = listed({"direction": "in"})
    assert sent_mail not in received                           # the mail the owner sent is left out
    assert set(ids["teams"]) <= received                       # but the whole chat stays, the owner's own line too
    teams_in = listed({"direction": "in", "teams_direction": "in"})
    assert mine not in teams_in and set(ids["teams"]) - {mine} <= teams_in
    assert listed({"teams_direction": "out"}) & set(ids["teams"]) == {mine}
    assert sent_mail in listed({"teams_direction": "out"})    # Teams' direction leaves mail both ways
    threads = c.get("/api/threads", params={"direction": "in"}).json()
    assert threads["total"] >= 1                               # conversations take the same filters
