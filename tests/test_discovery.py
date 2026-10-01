"""Discovery review (talos.discovery, Operations › Discovery): loading the drafts, the owner's decisions, and
the binders accepting makes. The drafts here are invented; the owner's real ones live in TALOS_HOME/config/discovery,
outside the repository."""

import json

import pytest
from psycopg.types.json import Jsonb
from test_gold import POST
from test_web import client

from talos import discovery, objects

SYSTEMS = {
    "schema": "talos.discovery/1", "draft": "systems", "generated": "2026-09-26",
    "status_values": {"active": "operational evidence in the last ~12 months",
                      "probably_retired": "operational mail stopped; only history or marketing since"},
    "items": [
        {"key": "unifi", "kind": "system", "name": "UniFi", "category": "network", "vendor": "Ubiquiti",
         "status": "active", "first_seen": "2019-02-01", "last_seen": "2026-09-20",
         "last_mail_from_own_senders": None, "used_for": "Office wifi and switches.",
         "related": ["cloudflare"], "evidence": {"work_mail_from_system": 40, "teams_messages": 12},
         "examples": ["UniFi alert: AP offline"], "suggested_collector": "A read-only UniFi API user.",
         "open_questions": ["Is the Cloud Key replaced?"], "existing_object_id": None, "cleanup": None,
         "noise": None, "decision": None, "correction": None},
        {"key": "cloudflare", "kind": "system", "name": "Cloudflare", "category": "network", "vendor": "Cloudflare",
         "status": "active", "first_seen": "2021-11-10", "last_seen": "2026-09-22", "used_for": "DNS and CDN.",
         "related": ["unifi"], "evidence": {"work_mail_from_system": 118}, "examples": [],
         "suggested_collector": "A read-only API token.", "open_questions": [], "existing_object_id": None,
         "decision": None, "correction": None},
        {"key": "veeam", "kind": "system", "name": "Veeam Backup & Replication", "category": "backup",
         "vendor": "Veeam", "status": "probably_retired", "first_seen": "2016-01-01", "last_seen": "2022-03-21",
         "last_mail_from_own_senders": "2022-03-21", "used_for": "Backups of the old VMware hosts.",
         "related": [], "evidence": {"work_mail_from_system": 10741},
         "cleanup": {"mail": 10741, "senders": ["veeam@company.example"]},
         "suggested_collector": "None: retired.", "open_questions": [], "existing_object_id": None,
         "decision": None, "correction": None},
        {"key": "asa", "kind": "system", "name": "Cisco ASA firewall (edge-fw)", "category": "network",
         "status": "probably_retired", "used_for": "The old firewall.", "evidence": {}, "related": [],
         "decision": None, "correction": None},
        {"key": "tictac", "kind": "system", "name": "TicTac", "category": "hr", "status": "unclear",
         "used_for": "Time reporting.", "evidence": {}, "related": [], "decision": None, "correction": None},
    ]}
CANDIDATES = {
    "schema": "talos.discovery/1", "draft": "candidates", "generated": "2026-09-26",
    "items": [
        {"key": "area:network", "rank": 5, "kind": "area", "name": "Network", "suggested_parent": "top level",
         "sphere": "work", "why": "Firewalls, wifi and DNS.", "evidence": {"assigned_messages": 6187},
         "confidence": "high", "decision": None, "correction": None},
        {"key": "topic:licences", "rank": 10, "kind": "topic", "name": "Licences & Renewals",
         "suggested_parent": "Costs", "sphere": "work", "why": "Renewals and invoices.",
         "evidence": {"assigned_messages": 3674}, "confidence": "high", "decision": None, "correction": None},
    ]}
SETUP = {
    "schema": "talos.discovery/1", "draft": "my-setup", "generated": "2026-09-26", "host": "studio",
    "method": "read-only listing", "open_questions": ["Move the secrets to the Keychain?"],
    "items": [
        {"key": "cli:atuin", "kind": "cli", "group": "Shell and utilities", "name": "atuin",
         "what": "shell history sync", "decision": None, "correction": None},
        {"key": "cli:jq", "kind": "cli", "group": "Shell and utilities", "name": "jq", "what": "JSON on the shell",
         "decision": None, "correction": None},
        {"key": "application:steam", "kind": "application", "group": "Games", "name": "Steam", "what": "games",
         "decision": None, "correction": None},
    ]}


def write(folder, systems=SYSTEMS, candidates=CANDIDATES, setup=SETUP):
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in (("systems", systems), ("candidates", candidates), ("my-setup", setup)):
        (folder / f"{name}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return folder


@pytest.fixture
def drafts(tmp_path):
    return write(tmp_path / "discovery")


def item(conn, key):
    return conn.execute("select * from discovery_item where item_key = %s", (key,)).fetchone()


def binder(conn, oid):
    return objects.get(conn, oid)


def test_loading_is_idempotent_and_refreshes_the_draft(conn, drafts):
    first = discovery.load(conn, drafts)
    assert first["systems"]["new"] == 5 and first["candidates"]["new"] == 2 and first["my-setup"]["new"] == 3
    again = discovery.load(conn, drafts)
    assert again["systems"] == {"items": 5, "new": 0, "updated": 0, "unchanged": 5, "decisions_from_file": 0,
                                "decisions_kept": 0, "gone": []}
    assert conn.execute("select count(*) n from discovery_item").fetchone()["n"] == 10

    changed = json.loads(json.dumps(SYSTEMS))
    changed["items"][0]["used_for"] = "Office wifi, switches and cameras."
    del changed["items"][4]
    report = discovery.load(conn, write(drafts, systems=changed))
    assert report["systems"]["updated"] == 1 and report["systems"]["gone"] == ["tictac"]
    assert item(conn, "unifi")["payload"]["used_for"] == "Office wifi, switches and cameras."
    assert item(conn, "tictac") is not None  # left the file, kept in the table


def test_without_a_folder_the_drafts_come_from_the_owners_config_folder_and_a_missing_one_is_reported(conn, tmp_path,
                                                                                               monkeypatch):
    monkeypatch.setenv("TALOS_HOME", str(tmp_path))
    assert {k: "missing" in v for k, v in discovery.load(conn).items()} == {s: True for s in discovery.SOURCES}
    write(tmp_path / "config" / "discovery")
    report = discovery.load(conn)
    assert report["systems"]["items"] == len(SYSTEMS["items"]) and report["candidates"]["items"] > 0
    assert discovery.items(conn)["sources"]["systems"]["decided"] == 0


def test_a_decision_is_never_overwritten_by_a_load(conn, drafts):
    discovery.load(conn, drafts)
    discovery.decide(conn, [item(conn, "tictac")["id"]], "reject")
    in_file = json.loads(json.dumps(SYSTEMS))
    in_file["items"][4]["decision"] = "accept"                     # the file says otherwise
    in_file["items"][1]["decision"] = "reject"                     # a decision made in the file by hand
    in_file["items"][1]["correction"] = {"note": "we moved DNS"}
    report = discovery.load(conn, write(drafts, systems=in_file))
    assert report["systems"]["decisions_kept"] == 1 and report["systems"]["decisions_from_file"] == 1
    assert item(conn, "tictac")["decision"] == "reject" and item(conn, "tictac")["object_id"] is None
    assert item(conn, "cloudflare")["decision"] == "reject"
    assert item(conn, "cloudflare")["correction"] == {"note": "we moved DNS"}


def test_accepting_a_system_makes_a_system_binder_with_its_body_and_attrs(conn, drafts):
    discovery.load(conn, drafts)
    [row] = discovery.decide(conn, [item(conn, "unifi")["id"]], "accept")
    o = binder(conn, row["object_id"])
    assert (o["kind"], o["name"], o["description"]) == ("system", "UniFi", "Office wifi and switches.")
    assert o["origin"]["uid"] == "discovery:unifi"
    assert {k: o["attrs"][k] for k in ("status", "vendor", "category", "first_seen", "last_seen")} == {
        "status": "active", "vendor": "Ubiquiti", "category": "network",
        "first_seen": "2019-02-01", "last_seen": "2026-09-20"}
    headings = [line for line in o["body"].splitlines() if line.startswith("## ")]
    assert headings == ["## Purpose", "## How it's used", "## Evidence", "## Related systems",
                        "## How Talos could document it", "## Open questions"]
    assert "- Cloudflare" in o["body"] and "A read-only UniFi API user." in o["body"]
    assert "Is the Cloud Key replaced?" in o["body"] and "“UniFi alert: AP offline”" in o["body"]
    assert objects.purpose(o["body"]) == o["description"]

    # Accepting again makes nothing new.
    discovery.decide(conn, [item(conn, "unifi")["id"]], "accept")
    assert conn.execute("select count(*) n from object").fetchone()["n"] == 1


def test_editing_before_accepting_changes_the_binders_name_kind_and_description(conn, drafts):
    discovery.load(conn, drafts)
    [row] = discovery.decide(conn, [item(conn, "unifi")["id"]], "accept",
                             correction={"name": "UniFi (office)", "description": "The office network.",
                                         "kind": "system"})
    o = binder(conn, row["object_id"])
    assert (o["name"], o["description"]) == ("UniFi (office)", "The office network.")
    assert row["correction"] == {"name": "UniFi (office)", "description": "The office network.", "kind": "system"}
    with pytest.raises(discovery.DiscoveryError):
        discovery.decide(conn, [item(conn, "unifi")["id"]], "accept", correction={"kind": "planet"})


def test_an_existing_binder_of_the_same_name_is_enriched_not_duplicated(conn, drafts):
    existing = objects.create(conn, "system", "UniFi", attrs={"lifecycle": "active", "vendor": "Ubiquiti Inc."})
    objects.set_body(conn, existing, "## Purpose\n\nThe vault's own words.\n")
    conn.execute("update object set origin = %s where id = %s",
                 (Jsonb({"source": "obsidian", "uid": "vault-uid-1"}), existing))
    vpn = objects.create(conn, "system", "Cisco ASA firewall")
    discovery.load(conn, drafts)
    [row] = discovery.decide(conn, [item(conn, "unifi")["id"]], "accept")
    assert row["object_id"] == existing
    assert conn.execute("select count(*) n from object where kind = 'system'").fetchone()["n"] == 2
    o = binder(conn, existing)
    assert o["origin"]["uid"] == "vault-uid-1"                               # the vault's key is kept
    assert o["body"] == "## Purpose\n\nThe vault's own words.\n"             # its body untouched
    assert o["attrs"]["vendor"] == "Ubiquiti Inc."                           # a filled attr is kept
    assert o["attrs"]["status"] == "active" and o["attrs"]["first_seen"] == "2019-02-01"  # empty ones filled
    [n] = conn.execute("select title, body, kind from note where object_id = %s", (existing,)).fetchall()
    assert n["title"] == "Discovery draft: UniFi" and "## Evidence" in n["body"] and n["kind"] == "discovery"
    discovery.decide(conn, [row["id"]], "accept")
    assert conn.execute("select count(*) n from note").fetchone()["n"] == 1

    # A draft name with a parenthesis finds the binder without it.
    [asa] = discovery.decide(conn, [item(conn, "asa")["id"]], "accept")
    assert asa["object_id"] == vpn


def test_the_drafts_existing_object_id_links_a_binder_with_a_shorter_name(conn, drafts):
    cp = objects.create(conn, "system", "Check Point")
    systems = json.loads(json.dumps(SYSTEMS))
    systems["items"].append({"key": "checkpoint", "kind": "system", "name": "Check Point firewalls",
                             "status": "active", "used_for": "Firewalls.", "related": [], "evidence": {},
                             "existing_object_id": cp, "decision": None, "correction": None})
    discovery.load(conn, write(drafts, systems=systems))
    [row] = discovery.decide(conn, [item(conn, "checkpoint")["id"]], "accept")
    assert row["object_id"] == cp


def test_bulk_accepts_active_systems_and_rejects_retired_ones_without_binders(conn, drafts):
    discovery.load(conn, drafts)
    discovery.decide(conn, [item(conn, "cloudflare")["id"]], "reject")      # decided: left alone
    res = discovery.bulk(conn, "accept_active")
    assert res["count"] == 1
    assert item(conn, "unifi")["decision"] == "accept" and item(conn, "unifi")["object_id"]
    assert item(conn, "cloudflare")["decision"] == "reject" and item(conn, "cloudflare")["object_id"] is None

    res = discovery.bulk(conn, "reject_retired")
    assert res["count"] == 2
    for key in ("veeam", "asa"):
        row = item(conn, key)
        assert row["decision"] == "reject" and row["correction"] == {"status": "retired"} and row["object_id"] is None
    assert item(conn, "tictac")["decision"] is None
    assert conn.execute("select count(*) n from object").fetchone()["n"] == 1


def test_selected_rows_are_decided_together(conn, drafts):
    discovery.load(conn, drafts)
    ids = [item(conn, k)["id"] for k in ("unifi", "cloudflare", "tictac")]
    rows = discovery.decide(conn, ids, "accept")
    assert {r["decision"] for r in rows} == {"accept"} and len({r["object_id"] for r in rows}) == 3
    rows = discovery.decide(conn, ids[:2], None)                              # undone; the binders stay
    assert {r["decision"] for r in rows} == {None} and all(r["object_id"] for r in rows)
    assert conn.execute("select count(*) n from object").fetchone()["n"] == 3


def test_a_candidate_becomes_a_binder_of_its_kind_under_its_parent(conn, drafts):
    costs = objects.create(conn, "area", "Costs")
    discovery.load(conn, drafts)
    [area] = discovery.decide(conn, [item(conn, "area:network")["id"]], "accept")
    [topic] = discovery.decide(conn, [item(conn, "topic:licences")["id"]], "accept")
    a, t = binder(conn, area["object_id"]), binder(conn, topic["object_id"])
    assert (a["kind"], a["name"]) == ("area", "Network") and (t["kind"], t["name"]) == ("topic", "Licences & Renewals")
    assert a["origin"]["uid"] == "discovery:area:network"
    assert [p["id"] for p in objects.objects_of(conn, t["id"])] == [costs]
    assert objects.objects_of(conn, a["id"]) == []                            # "top level" is no binder
    assert "## Purpose" in t["body"] and "Renewals and invoices." in t["body"]


def test_a_candidate_edited_to_another_kind_becomes_that_kind(conn, drafts):
    discovery.load(conn, drafts)
    [row] = discovery.decide(conn, [item(conn, "area:network")["id"]], "accept",
                             correction={"kind": "topic", "name": "Networking"})
    o = binder(conn, row["object_id"])
    assert (o["kind"], o["name"]) == ("topic", "Networking")


def test_a_retired_system_can_be_kept_as_a_retired_binder_on_reject(conn, drafts):
    discovery.load(conn, drafts)
    [plain] = discovery.decide(conn, [item(conn, "asa")["id"]], "reject")
    assert plain["object_id"] is None and plain["correction"] is None
    [kept] = discovery.decide(conn, [item(conn, "veeam")["id"]], "reject", keep_retired=True)
    assert kept["decision"] == "reject" and kept["correction"] == {"status": "retired", "keep_as_binder": True}
    o = binder(conn, kept["object_id"])
    assert o["kind"] == "system" and o["attrs"]["lifecycle"] == "retired" and o["attrs"]["status"] == "retired"
    assert "veeam@company.example" in o["body"]
    with pytest.raises(discovery.DiscoveryError):
        discovery.decide(conn, [item(conn, "area:network")["id"]], "reject", keep_retired=True)


def test_setup_items_become_group_notes_on_one_binder(conn, drafts):
    discovery.load(conn, drafts)
    rows = discovery.decide(conn, [item(conn, k)["id"] for k in ("cli:atuin", "application:steam")], "accept")
    [oid] = {r["object_id"] for r in rows}
    o = binder(conn, oid)
    assert (o["kind"], o["name"]) == ("topic", "My setup (Studio)")
    assert "Move the secrets to the Keychain?" in o["body"]
    notes = {n["title"]: n for n in conn.execute("select * from note where object_id = %s", (oid,))}
    assert set(notes) == {"Shell and utilities", "Games"}
    assert notes["Shell and utilities"]["body"] == "- **atuin** (CLI tool): shell history sync"
    discovery.decide(conn, [item(conn, "cli:jq")["id"]], "accept")
    body = conn.execute("select body from note where id = %s", (notes["Shell and utilities"]["id"],)).fetchone()["body"]
    assert body.splitlines() == ["- **atuin** (CLI tool): shell history sync", "- **jq** (CLI tool): JSON on the shell"]
    assert conn.execute("select count(*) n from object").fetchone()["n"] == 1

    # A note the owner edited is only added to, never rewritten.
    conn.execute("update note set body = 'My own words' where id = %s", (notes["Games"]["id"],))
    discovery.decide(conn, [item(conn, "application:steam")["id"]], None)
    discovery.decide(conn, [item(conn, "application:steam")["id"]], "accept")
    assert conn.execute("select body from note where id = %s", (notes["Games"]["id"],)).fetchone()["body"] == \
        "My own words\n- **Steam** (App): games"


def test_export_writes_the_decisions_back_and_changes_nothing_else(conn, drafts, tmp_path):
    before = (drafts / "systems.json").read_text(encoding="utf-8")
    discovery.load(conn, drafts)
    assert discovery.export(conn, drafts)["systems"] == 0
    assert (drafts / "systems.json").read_text(encoding="utf-8") == before
    discovery.decide(conn, [item(conn, "unifi")["id"]], "accept", correction={"note": "a UDM Pro now"})
    out = tmp_path / "out"
    assert discovery.export(conn, drafts, out=out)["systems"] == 1
    data = json.loads((out / "systems.json").read_text(encoding="utf-8"))
    assert data["items"][0]["decision"] == "accept" and data["items"][0]["correction"] == {"note": "a UDM Pro now"}
    assert data["items"][1]["decision"] is None
    assert (drafts / "systems.json").read_text(encoding="utf-8") == before


def test_the_api_lists_decides_and_bulk_decides(conn, drafts, vault, database):
    discovery.load(conn, drafts)
    conn.commit()
    c = client(database, vault)
    d = c.get("/api/discovery").json()
    assert d["sources"]["systems"]["total"] == 5 and len(d["items"]) == 10
    unifi = next(x for x in d["items"] if x["item_key"] == "unifi")
    assert unifi["related_names"] == ["Cloudflare"]
    assert c.post("/api/discovery/decide", json={"ids": [unifi["id"]], "decision": "accept"}).status_code == 403
    r = c.post("/api/discovery/decide", json={"ids": [unifi["id"]], "decision": "accept"}, headers=POST)
    assert r.status_code == 200 and r.json()["items"][0]["object_id"]
    bad = c.post("/api/discovery/decide", json={"ids": [unifi["id"]], "decision": "maybe"}, headers=POST)
    assert bad.status_code == 400
    r = c.post("/api/discovery/bulk", json={"action": "reject_retired"}, headers=POST)
    assert r.json()["count"] == 2
    assert c.get("/api/discovery").json()["sources"]["systems"]["decided"] == 3
