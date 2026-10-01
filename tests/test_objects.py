from datetime import datetime, timezone

import mailfactory as mf
import pytest
from starlette.testclient import TestClient

from talos import objects, rules
from talos.config import Settings
from talos.ingest import Location
from talos.web import app


def ingest(ingestor, key, raw, labels=(), folder="[all]"):
    return ingestor.ingest("gmail", raw, Location(folder, key, provider_id=key, labels=list(labels))).message_id


def thread_of(conn, message_id):
    return conn.execute("select thread_id from message where id = %s", (message_id,)).fetchone()["thread_id"]


def member_ids(conn, oid, **kw):
    return {r["entity_id"] for r in objects.members(conn, oid, **kw)["rows"]}


def test_an_exclusion_beats_a_rule_membership_and_survives_a_rule_rerun(conn, ingestor):
    a = ingest(ingestor, "a", mf.make(frm="Klarna <noreply@klarna.com>", subject="Kvitto 1"))
    b = ingest(ingestor, "b", mf.make(frm="Klarna <noreply@klarna.com>", subject="Kvitto 2"))
    oid = objects.create(conn, "collection", "Receipts")
    rules.save(conn, rules.Rule("klarna", "Klarna", [{"field": "from_domain", "op": "is", "value": "klarna.com"}],
                                {"object": oid}))
    rules.run_all(conn)
    res = objects.members(conn, oid)
    assert {r["entity_id"] for r in res["rows"]} == {a, b}
    assert all(r["sources"] == ["rule:klarna@1"] for r in res["rows"])

    objects.exclude(conn, oid, [b])
    rules.run_all(conn)
    assert member_ids(conn, oid) == {a}
    assert [r["entity_id"] for r in objects.excluded(conn, oid)] == [b]
    assert objects.objects_of(conn, b) == []

    # Adding by hand is the later human decision, so it clears the exclusion.
    objects.add(conn, oid, [b])
    res = {r["entity_id"]: r["sources"] for r in objects.members(conn, oid)["rows"]}
    assert res[b] == ["human", "rule:klarna@1"]
    assert objects.excluded(conn, oid) == []


def test_query_membership_is_live_so_new_mail_joins_without_a_rerun(conn, ingestor):
    ingest(ingestor, "a", mf.make(frm="Anna <anna@nordvik.se>", subject="Offert"))
    oid = objects.create(conn, "project", "Nordvik",
                         query=[{"field": "from_domain", "op": "is", "value": "nordvik.se"}])
    assert objects.members(conn, oid)["total"] == 1
    later = ingest(ingestor, "b", mf.make(frm="Oskar <oskar@nordvik.se>", subject="Ny offert"))
    ingest(ingestor, "c", mf.make(frm="Other <x@elsewhere.se>", subject="Unrelated"))
    res = objects.members(conn, oid)
    assert res["total"] == 2 and later in {r["entity_id"] for r in res["rows"]}
    assert {tuple(r["sources"]) for r in res["rows"]} == {("query",)}
    assert [o["sources"] for o in objects.objects_of(conn, later)] == [["query"]]

    objects.exclude(conn, oid, [later])
    assert objects.members(conn, oid)["total"] == 1
    objects.set_query(conn, oid, None)
    assert objects.members(conn, oid)["total"] == 0


def test_nesting_includes_the_children_members_only_when_recursive(conn, ingestor):
    m1 = ingest(ingestor, "a", mf.make(subject="Brandvägg"))
    m2 = ingest(ingestor, "b", mf.make(frm="Eva <eva@work.example>", subject="Loggar"))
    m3 = ingest(ingestor, "c", mf.make(frm="Per <per@work.example>", subject="Larm"))
    parent = objects.create(conn, "project", "Security observability")
    child = objects.create(conn, "collection", "Firewall")
    grandchild = objects.create(conn, "collection", "Alarms")
    objects.add(conn, parent, [child, m1])
    objects.add(conn, child, [m2, grandchild])
    objects.add(conn, grandchild, [m3])

    assert member_ids(conn, parent) == {child, m1}
    deep = {r["entity_id"]: r for r in objects.members(conn, parent, recursive=True)["rows"]}
    assert set(deep) == {child, m1, m2, grandchild, m3}
    assert deep[m3]["via"] == [grandchild] and deep[m1]["via"] == []
    assert objects.members(conn, parent, "message", recursive=True)["total"] == 3

    # An exclusion on the parent also holds for what arrives through a child.
    objects.exclude(conn, parent, [m3])
    assert m3 not in member_ids(conn, parent, recursive=True)
    # Nesting can never loop.
    with pytest.raises(objects.ObjectError):
        objects.add(conn, grandchild, [parent])
    with pytest.raises(objects.ObjectError):
        objects.add(conn, parent, [parent])
    assert [o["name"] for o in objects.objects_of(conn, child)] == ["Security observability"]


def test_common_finds_the_org_shared_across_threads(conn, ingestor):
    senders = ["Anna <anna@nordvik.se>", "Oskar <oskar@nordvik.se>", "Lena <lena@nordvik.se>"]
    threads = []
    for i, frm in enumerate(senders):
        mid = ingest(ingestor, f"t{i}", mf.make(
            frm=frm, cc="Eva Berg <eva@partner.example>" if i == 0 else None, subject=f"Ärende {i}",
            date=datetime(2026, 9, 10 + i, 9, 0, tzinfo=timezone.utc),
            attachments=[("offert.pdf", "application/pdf", mf.pdf("Offert"))] if i < 2 else ()),
            labels=["Kunder", "\\Inbox"] if i < 2 else ["Kunder"])
        threads.append(thread_of(conn, mid))
    stray = ingest(ingestor, "s", mf.make(frm="Nyhetsbrev <news@shop.example>", subject="Rea"))
    oid = objects.create(conn, "project", "Nordvik")
    objects.add(conn, oid, [*threads, stray])

    c = objects.common(conn, oid)
    assert c["messages"] == 4 and c["threads"] == 4
    assert c["orgs"][0]["domain"] == "nordvik.se"
    assert (c["orgs"][0]["threads"], c["orgs"][0]["people"]) == (3, 3)
    assert "owner@gmail.com" not in {p["address"] for p in c["people"]}  # the owner is on everything
    assert c["labels"][0] == {"label": "Kunder", "messages": 3}
    assert c["folders"][0]["folder"] == "[all]"
    assert c["attachment_types"][0]["content_type"] == "application/pdf" and c["attachment_types"][0]["n"] == 2
    assert c["first_at"].day == 10 and c["last_at"] > c["first_at"]
    assert objects.common(conn, objects.create(conn, "case", "Empty"))["messages"] == 0


def test_promoting_an_org_makes_a_live_project_once(conn, ingestor):
    ingest(ingestor, "a", mf.make(frm="Anna <anna@nordvik.se>"))
    org = conn.execute("select id from org where domain = 'nordvik.se'").fetchone()["id"]
    oid = objects.promote_org(conn, org)
    assert objects.promote_org(conn, org) == oid
    obj = objects.get(conn, oid)
    assert obj["kind"] == "project" and obj["query"] == [{"field": "from_domain", "op": "is", "value": "nordvik.se"}]
    assert objects.members(conn, oid)["counts"] == {"message": 1, "org": 1}
    assert [o["id"] for o in objects.objects_of(conn, org)] == [oid]


def test_objects_are_renamed_described_and_archived_and_bad_input_is_refused(conn):
    oid = objects.create(conn, "area", "Försäkring")
    objects.rename(conn, oid, "Insurance")
    objects.describe(conn, oid, "Policies and renewals")
    assert (objects.get(conn, oid)["name"], objects.get(conn, oid)["description"]) == ("Insurance", "Policies and renewals")
    objects.archive(conn, oid)
    assert objects.list_objects(conn) == [] and len(objects.list_objects(conn, include_archived=True)) == 1
    for bad in (lambda: objects.create(conn, "folder", "x"), lambda: objects.create(conn, "case", " "),
                lambda: objects.create(conn, "case", "x", query=[{"field": "nope", "op": "is", "value": 1}]),
                lambda: objects.add(conn, oid, [999999])):
        with pytest.raises(objects.ObjectError):
            bad()


def test_the_message_drawer_shows_objects_reached_through_the_thread(conn, ingestor):
    first = ingest(ingestor, "a", mf.make(subject="Fönster", msgid="<a@test.invalid>"))
    reply = ingest(ingestor, "b", mf.make(frm="Eva <eva@work.example>", subject="Re: Fönster",
                                          in_reply_to="<a@test.invalid>", msgid="<b@test.invalid>"))
    oid = objects.create(conn, "case", "Firewall window")
    objects.add(conn, oid, [thread_of(conn, first)])
    [row] = objects.objects_of(conn, reply)
    assert row["id"] == oid and row["sources"] == [] and row["thread_sources"] == ["human"]
    assert set(objects.message_ids(conn, oid)) == {first, reply}
    objects.exclude(conn, oid, [reply])
    assert objects.objects_of(conn, reply) == [] and objects.message_ids(conn, oid) == [first]


def client(database, vault):
    return TestClient(app.create(Settings(home=vault.root.parent, dsn=database),
                                  allowed_hosts=["testserver"]))


def test_the_api_creates_objects_and_changes_members_only_with_the_header(conn, ingestor, vault, database):
    mid = ingest(ingestor, "a", mf.make(frm="Anna <anna@nordvik.se>", subject="Offert"))
    conn.commit()
    c = client(database, vault)
    h = {"X-Talos": "1"}

    assert c.post("/api/objects", json={"kind": "project", "name": "Nordvik"}).status_code == 403
    assert c.get("/api/objects").json() == []
    r = c.post("/api/objects", json={"kind": "project", "name": "Nordvik"}, headers=h)
    assert r.status_code == 201
    oid = r.json()["id"]
    assert c.post("/api/objects", json={"kind": "nonsense", "name": "x"}, headers=h).status_code == 400

    assert c.post(f"/api/objects/{oid}/members", json={"action": "add", "ids": [mid]}).status_code == 403
    assert c.post(f"/api/objects/{oid}", json={"name": "Hijacked"}).status_code == 403
    assert objects.members(conn, oid)["total"] == 0
    r = c.post(f"/api/objects/{oid}/members", json={"action": "add", "ids": [mid]}, headers=h)
    assert r.status_code == 200 and r.json()["counts"] == {"message": 1}
    assert c.post(f"/api/objects/{oid}/members", json={"action": "drop", "ids": [mid]}, headers=h).status_code == 400

    d = c.get(f"/api/objects/{oid}").json()
    assert d["name"] == "Nordvik" and d["members"]["rows"][0]["sources"] == ["human"]
    assert d["common"]["orgs"][0]["domain"] == "nordvik.se"
    assert c.get("/api/objects").json()[0]["members"] == 1
    assert [o["id"] for o in c.get(f"/api/messages/{mid}").json()["objects"]] == [oid]

    rules.save(conn, rules.Rule("nordvik", "Nordvik", [{"field": "from_domain", "op": "is", "value": "nordvik.se"}],
                                {"object": oid}))
    rules.run_all(conn)
    conn.commit()
    assert c.get("/api/rules").json()[0]["hits"] == 1  # rule-made memberships count as hits

    org = d["common"]["orgs"][0]["id"]
    promoted = c.post("/api/objects", json={"promote_org": org}, headers=h).json()
    assert promoted["query"][0]["value"] == "nordvik.se"
    assert c.post(f"/api/objects/{oid}", json={"archived": True}, headers=h).json()["archived"] is True
    assert c.get("/api/objects/424242").status_code == 404
