"""The work space (talos.space) and Discover (talos.discover): binders' mail, watchers, insights."""

from datetime import timedelta

import mailfactory as mf
import pytest
from test_messages_rules import NOW
from test_web import client

from talos import discover, objects, space
from talos.ingest import Location


def _msg(ingestor, *, frm="Oskar Nyström <oskar@nordvik.se>", subject="Hej", body="Hej", days_ago=1, key=None,
         thread=None, account="gmail", to=mf.ME):
    key = key or f"{frm}-{subject}-{days_ago}"
    at = NOW - timedelta(days=days_ago)
    raw = mf.make(frm=frm, to=to, subject=subject, body=body, date=at, msgid=f"<{abs(hash(key))}@test.invalid>")
    return ingestor.ingest(account, raw, Location("[all]", key, provider_thread_id=thread, received_at=at)).message_id


def _thread(conn, mid):
    return conn.execute("select thread_id from message where id = %s", (mid,)).fetchone()["thread_id"]


def test_a_binders_terms_are_phrases_and_found_mail_leaves_out_its_members(conn, ingestor):
    cp = _msg(ingestor, subject="Brandvägg", body="Check Point larmar om en infekterad enhet", key="cp")
    other = _msg(ingestor, subject="Villkor", body="Check the new terms. Point by point.", key="other")
    old = _msg(ingestor, subject="Förnyelse", body="Avtalet för Check Point förnyas", key="old", days_ago=40)
    b = objects.create(conn, "system", "Check Point")
    conn.commit()
    f = space.found(conn, b)
    assert {r["message_id"] for r in f["rows"]} == {cp, old}  # a phrase, not two words anywhere
    assert other not in {r["message_id"] for r in f["rows"]} and f["threads"] == 2
    objects.add(conn, b, [_thread(conn, cp)])
    objects.exclude(conn, b, [_thread(conn, old)])
    assert space.found(conn, b)["rows"] == []  # one is in, the other kept out
    assert space.set_terms(conn, b, ["Brandvägg", " brandvägg ", ""]) == ["Brandvägg"]
    assert space.terms(objects.get(conn, b)) == ["Brandvägg"]
    assert space.set_terms(conn, b, []) == ["Check Point"]  # empty: the name again


def test_the_work_space_puts_binders_in_their_areas_and_counts_their_mail(conn, ingestor):
    _msg(ingestor, body="Om UniFi-nätet på kontoret", key="u1")
    _msg(ingestor, body="UniFi igen", key="u2", days_ago=100)
    area = objects.create(conn, "area", "Security")
    unifi = objects.create(conn, "system", "UniFi")
    loose = objects.create(conn, "project", "Something new")
    objects.add(conn, area, [unifi])  # member_of, as "Part of" makes it
    conn.commit()
    o = space.overview(conn)
    a = next(x for x in o["areas"] if x["id"] == area)
    assert [b["name"] for b in a["binders"]] == ["UniFi"]
    assert a["binders"][0]["mentions"] == 2 and a["binders"][0]["recent"] == 1 and a["recent"] == 1
    assert [b["id"] for b in o["loose"]] == [loose]


def test_neighbours_are_linked_binders_and_binders_sharing_threads(conn, ingestor):
    for i in range(4):
        _msg(ingestor, subject=f"Nät {i}", body="Check Point och UniFi i samma tråd", key=f"n{i}")
    cp = objects.create(conn, "system", "Check Point")
    objects.create(conn, "system", "UniFi")  # named in the same threads as Check Point
    azure = objects.create(conn, "system", "Azure")
    conn.execute("insert into edge (src, rel, dst, source) values (%s, 'related', %s, 'import:vault')", (azure, cp))
    conn.commit()
    n = {x["name"]: x for x in space.neighbours(conn, cp)}
    assert n["UniFi"]["shared"] == 4 and n["Azure"]["why"] == "linked"


def test_a_watcher_counts_what_is_new_and_its_binder_takes_it_in(conn, ingestor):
    b = objects.create(conn, "project", "FortiDLP Renewal")
    conn.commit()
    with pytest.raises(space.SpaceError):
        space.add_watcher(conn, "Everything", "")  # nothing to look for
    w = space.add_watcher(conn, "FortiDLP", "q=FortiDLP", object_id=b)
    conn.execute("update watcher set seen_at = %s where id = %s", (NOW - timedelta(days=5), w))
    _msg(ingestor, body="FortiDLP förnyelse", key="f1", days_ago=10)
    new = _msg(ingestor, body="FortiDLP offert", key="f2", days_ago=1)
    conn.commit()
    got = space.watchers(conn)[0]
    assert (got["total"], got["fresh"], got["object_name"]) == (2, 1, "FortiDLP Renewal")
    assert space.take_in(conn, w) == 1
    assert new in objects.message_ids(conn, b) and space.watchers(conn)[0]["fresh"] == 0
    space.update_watcher(conn, w, paused=True)
    assert space.watchers(conn)[0]["paused"]
    space.remove_watcher(conn, w)
    assert space.watchers(conn) == []


def test_an_aggregation_counts_a_selection_in_groups(conn, ingestor):
    for i in range(3):
        _msg(ingestor, frm=f"Säljare <s{i}@vendor.example>", body="Köp vårt verktyg", key=f"v{i}")
    _msg(ingestor, frm="Annan <a@other.example>", body="Köp vårt verktyg", key="o")
    _msg(ingestor, frm="Annan <a@other.example>", body="Något annat", key="x")
    conn.commit()
    with pytest.raises(discover.DiscoverError):
        discover.add(conn, "All", "", "domain")
    with pytest.raises(discover.DiscoverError):
        discover.add(conn, "Bad", "q=verktyg", "colour")
    aid = discover.add(conn, "Tool sellers", "q=verktyg", "domain")
    a = discover.run(conn, next(x for x in discover.aggregations(conn) if x["id"] == aid))
    assert a["total"] == 4 and [(g["key"], g["n"]) for g in a["groups"]] == [("vendor.example", 3), ("other.example", 1)]
    assert a["groups"][0]["query"] == "q=verktyg&domain=vendor.example"
    discover.remove(conn, aid)
    assert discover.aggregations(conn) == []


def test_discover_finds_yearly_renewals_and_organisations_without_a_binder(conn, ingestor):
    # the database reads the month in local time (Europe/Stockholm): in the first hours of a month, UTC's is the last
    month = NOW.astimezone().month
    for years in (1, 2):
        at = NOW.replace(year=NOW.year - years)
        _msg(ingestor, frm="GoDaddy <renew@godaddy.example>", subject="Förnyelse av domänen",
             days_ago=(NOW - at).days, key=f"r{years}")
    for i in range(3):
        _msg(ingestor, frm="Alex <owner@gmail.com>", to="Pia <pia@arrow.example>",
             subject=f"Licenser {i}", key=f"out{i}", thread=f"t{i}")
    conn.commit()
    d = discover.compute(conn)
    ins = {i["key"]: i for i in d["insights"]}
    assert [x["label"] for x in ins["renewals"]["items"]] == ["godaddy.example"]
    assert ins["renewals"]["items"][0]["note"].startswith(discover.MONTHS[month - 1])
    assert [x["label"] for x in ins["no-binder"]["items"]] == ["arrow.example"]
    objects.create(conn, "project", "Arrow licences")
    conn.commit()
    assert discover.no_binder(conn)["items"] == []  # a binder names it now


def test_the_api_serves_the_work_space_and_discover_and_refuses_headerless_writes(conn, vault, database):
    b = objects.create(conn, "system", "UniFi")
    conn.commit()
    c = client(database, vault)
    assert c.get("/api/space").status_code == 200
    assert c.get(f"/api/space/binders/{b}").json()["found"]["terms"] == ["UniFi"]
    assert c.get("/api/discover").status_code == 200 and c.get("/api/aggregations").json()["rows"] == []
    assert c.post("/api/watchers", json={"name": "x", "query": "q=x"}).status_code == 403
    ok = c.post("/api/watchers", json={"name": "UniFi", "query": "q=UniFi", "object_id": b}, headers={"X-Talos": "1"})
    assert ok.status_code == 200
    assert c.get(f"/api/space/binders/{b}").json()["watchers"][0]["name"] == "UniFi"
    bad = c.post(f"/api/space/binders/{b}/found", json={"action": "delete", "thread_id": 1}, headers={"X-Talos": "1"})
    assert bad.status_code == 400
