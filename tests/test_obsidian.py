"""Lifting the Obsidian vault into Talos Web (talos.obsidian), on an invented vault."""

import hashlib
from datetime import UTC, datetime

import mailfactory as mf
import pytest
import vaultfactory as vf

from talos import obsidian, work
from talos.ingest import Location

HARBOUR = "Work/Projects/Harbour Migration/_home.md"
CUTOVER = "Work/Projects/Harbour Migration/Items/Confirm the cutover date.md"


@pytest.fixture
def vault_dir(tmp_path):
    return vf.build(tmp_path / "Talos")


@pytest.fixture
def cutover_mail(conn, ingestor):
    """The message the cutover item came from, in the work account and (a copy) in Gmail."""
    raw = mf.make(subject="Cutover dates", to="o@company.example", msgid=f"<{vf.MSGID}>")
    work = ingestor.ingest("work", raw, Location("Inbox", "c1")).message_id
    ingestor.ingest("gmail", raw, Location("[all]", "g1"))
    conn.commit()
    return work


def _obj(conn, name):
    return conn.execute("select * from object where name = %s", (name,)).fetchone()


def _item(conn, title):
    return conn.execute("select * from work_item where title = %s", (title,)).fetchone()


def _edges(conn, src, rel):
    return {r["name"] for r in conn.execute(
        "select coalesce(o.name, w.title, n.title) as name from edge e left join object o on o.id = e.dst"
        " left join work_item w on w.id = e.dst left join note n on n.id = e.dst"
        " where e.src = %s and e.rel = %s and e.source = 'import:vault'", (src, rel)).fetchall()}


def _snapshot(conn):
    q = {
        "object": "select id, kind, name, description, body, attrs, archived, origin, updated_at from object",
        "work_item": "select id, title, status, home_id, body, source, origin, updated_at from work_item",
        "event": "select id, work_item_id, field, by from work_item_event",
        "note": "select id, object_id, kind, title, body, origin, updated_at from note",
        "edge": "select src, rel, dst, source from edge",
    }
    return {k: sorted(map(repr, conn.execute(sql).fetchall())) for k, sql in q.items()}


def test_binders_items_and_notes_land_with_their_kinds_homes_and_relations(conn, vault_dir, cutover_mail):
    report = obsidian.import_vault(conn, vault_dir)
    kinds = {r["name"]: (r["kind"], r["archived"]) for r in conn.execute("select * from object").fetchall()}
    assert kinds == {"Harbour Migration": ("project", False), "Security": ("area", False),
                     "Backups": ("topic", False), "Nimbus NAS": ("system", False),
                     "Sourdough": ("personal_project", False), "Old Pilot": ("project", True)}
    assert report["counts"]["binder"]["created"] == 6

    harbour = _obj(conn, "Harbour Migration")
    assert harbour["origin"]["uid"] == "20260920T100000Z-project-harbour" and harbour["origin"]["path"] == HARBOUR
    assert harbour["description"] == "Move the file shares to the new harbour storage."
    assert "## What does not belong here" in harbour["body"] and "## Current context" in harbour["body"]
    assert "![[" not in harbour["body"] and "## Work" not in harbour["body"] and "# Harbour" not in harbour["body"]
    assert harbour["attrs"]["lifecycle"] == "active"
    assert harbour["attrs"]["canvases"]["overview.canvas"]["nodes"][0]["id"] == "purpose"
    assert _edges(conn, harbour["id"], "related") == {"Security", "Backups"}

    nas = _obj(conn, "Nimbus NAS")
    assert nas["attrs"]["state"] == {"health": "degraded", "version": "7.2", "update_available": True,
                                     "observed_at": "2026-09-20T09:00:00Z", "note": "One disk warns."}
    assert "## Health" not in nas["body"] and "## Authoritative sources" not in nas["body"]
    # A topic's `projects` list relates those projects to it.
    assert "Backups" in _edges(conn, harbour["id"], "related")

    cut = work.get(conn, _item(conn, "Confirm the cutover date")["id"])
    assert (cut["status"], cut["home_name"], str(cut["due"])) == ("next", "Harbour Migration", "2026-10-01")
    assert cut["body"].startswith("## Desired outcome") and "# Confirm" not in cut["body"]
    assert [r["name"] for r in cut["related"]] == ["Nimbus NAS"]
    assert cut["source"]["kind"] == "email" and cut["source"]["id"] == "4711"
    assert cut["origin"]["routed_by"] == "claude" and cut["origin"]["path"] == CUTOVER
    assert cut["created_at"] == datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    assert [(h["field"], h["by"]) for h in cut["history"]] == [("created", "import:vault")]
    assert _edges(conn, cut["id"], "receipt") == {"Routing receipt: 2026-09-21 Filing"}

    landlord = _item(conn, "Answer the landlord about the keys")
    assert landlord["status"] == "inbox" and landlord["home_id"] is None
    assert landlord["origin"]["suggested_home"] == vf.SECURITY
    assert landlord["origin"]["inbox_reason"].startswith("A guess")

    notes = {r["title"]: (r["kind"], r["object_id"]) for r in conn.execute("select * from note").fetchall()}
    assert notes["Kickoff decisions"] == ("decision", harbour["id"])
    assert notes["Recent state"] == ("state", nas["id"])
    assert notes["Authoritative sources"] == ("sources", nas["id"])
    assert notes["Disk failure"] == ("incident", nas["id"])
    assert notes["Routing receipt: 2026-09-21 Filing"] == ("receipt", harbour["id"])
    assert notes["Talos trial log"] == ("log", None)
    assert notes["Teams summary"] == ("observation", None) and notes["Teams"] == ("signal", None)
    assert notes["Inspiration"] == ("research", None)
    assert "Current state" not in notes            # State/current.md lives in the system's attrs


def test_a_spark_link_resolves_to_its_message_in_the_same_account(conn, vault_dir, cutover_mail):
    report = obsidian.import_vault(conn, vault_dir)
    cut = work.get(conn, _item(conn, "Confirm the cutover date")["id"])
    assert [m["id"] for m in cut["messages"]] == [cutover_mail]
    assert [w["title"] for w in work.of_message(conn, cutover_mail)] == ["Confirm the cutover date"]
    linked = {x["file"]: x for x in report["links"]["linked"]}
    assert linked["Confirm the cutover date.md"]["message_ids"] == [cutover_mail]
    missing = {x["file"]: x["reason"] for x in report["links"]["not_found"]}
    assert "not-in-talos@mail.test.invalid" in missing["Answer the landlord about the keys.md"]
    assert "meeting summary" in missing["Book the owners meeting.md"]
    assert "Teams message" in missing["Rotate the shared FTPS password.md"]
    assert "Evaluate the copy tool.md" in report["links"]["no_link"]


def test_a_teams_item_links_to_its_chat_message_by_chat_and_message_id(conn, vault_dir):
    mid = conn.execute("insert into entity (kind) values ('message') returning id").fetchone()["id"]
    conn.execute("insert into message (id, account_id, medium, provider_key, direction, parser_version)"
                 " values (%s, 'work', 'teams_chat', %s, 'out', 1)", (mid, f"chat:{vf.TEAMS_CHAT}:{vf.TEAMS_MSG}"))
    obsidian.import_vault(conn, vault_dir)
    item = work.get(conn, _item(conn, "Rotate the shared FTPS password")["id"])
    assert [m["id"] for m in item["messages"]] == [mid]


def test_the_spark_token_is_decoded_across_its_wrapped_lines():
    link = vf.spark_link("owner@company.example", "SA9PR18MB3775.namprd18.prod.outlook.com+x=y@z")
    assert "%0D%0A" in link
    assert obsidian.spark_token(link) == {"account": "owner@company.example",
                                          "message_id": "SA9PR18MB3775.namprd18.prod.outlook.com+x=y@z"}
    assert obsidian.spark_token("https://teams.microsoft.com/l/message/x") is None


def test_templates_placeholders_views_and_machinery_are_skipped_and_reported(conn, vault_dir):
    report = obsidian.import_vault(conn, vault_dir)
    skipped = {s["path"]: s["reason"] for s in report["skipped"]}
    assert skipped["Operations/Systems/_template/"] == "template folder"
    assert skipped["Work/Projects/Harbour Migration/_templates/"] == "template folder"
    assert skipped[".obsidian/"].startswith("Obsidian settings")
    assert skipped["Views/all-work.base"].startswith("Base")
    assert skipped["_system/History/changes.jsonl"].startswith("History change feed")
    assert "Home.md" in skipped and "Inbox/_home.md" in skipped and "_system/History/Changes.md" in skipped
    assert set(report["placeholders"]) == {"Work/Projects/Harbour Migration/Notes/Budget.md",
                                           "Work/Projects/Harbour Migration/Notes/Empty stub.md"}
    assert conn.execute("select count(*) as n from object where name = ''").fetchone()["n"] == 0
    assert not conn.execute("select 1 from note where title in ('Budget', 'Empty stub')").fetchone()
    text = obsidian.format_report(report)
    assert "iCloud placeholders" in text and "Budget.md" in text


def test_the_odd_todo_status_becomes_next_and_is_reported(conn, vault_dir):
    report = obsidian.import_vault(conn, vault_dir)
    assert _item(conn, "Evaluate the copy tool")["status"] == "next"
    assert {"path": "Work/Projects/Harbour Migration/Items/Evaluate the copy tool.md",
            "from": "todo", "to": "next"} in report["status_mapped"]


def test_archived_binders_are_archived_objects(conn, vault_dir):
    obsidian.import_vault(conn, vault_dir)
    old = _obj(conn, "Old Pilot")
    assert old["archived"] is True and old["origin"]["path"] == "Archive/Work/Old Pilot/_home.md"
    assert old["body"] == "## Purpose\n"


def test_a_rerun_changes_nothing(conn, vault_dir, cutover_mail):
    obsidian.import_vault(conn, vault_dir)
    conn.commit()
    before = _snapshot(conn)
    report = obsidian.import_vault(conn, vault_dir)
    assert _snapshot(conn) == before
    for what, c in report["counts"].items():
        assert c["created"] == c["updated"] == c["kept"] == 0, what
    assert report["counts"]["work_item"]["unchanged"] == 5
    assert len(report["links"]["linked"]) == 1 and not report["missing_from_vault"]


def test_a_rerun_after_the_owner_edits_an_item_in_talos_web_keeps_their_change(conn, vault_dir):
    obsidian.import_vault(conn, vault_dir)
    cut = _item(conn, "Confirm the cutover date")
    work.update(conn, cut["id"], status="doing")                     # the owner, in Talos Web
    # Meanwhile the vault changed both items.
    for rel in (CUTOVER, "Work/Projects/Harbour Migration/Items/Evaluate the copy tool.md"):
        path = vault_dir / rel
        path.write_text(path.read_text().replace("due: 2026-10-01", "due: 2026-10-08")
                        .replace("Something is decided.", "Something is decided, and written down."))
    report = obsidian.import_vault(conn, vault_dir)
    kept = _item(conn, "Confirm the cutover date")
    assert kept["status"] == "doing" and str(kept["due"]) == "2026-10-01"
    assert report["kept_your_change"] == [CUTOVER]
    assert report["counts"]["work_item"]["kept"] == 1 and report["counts"]["work_item"]["updated"] == 1
    tool = work.get(conn, _item(conn, "Evaluate the copy tool")["id"])
    assert tool["body"].endswith("Something is decided, and written down.\n")
    assert [(h["field"], h["by"]) for h in tool["history"]] == [("created", "import:vault"), ("body", "import:vault")]
    # A binder renamed in Talos Web is kept too, once the vault changes it.
    harbour = _obj(conn, "Harbour Migration")
    conn.execute("update object set name = 'Harbour cutover' where id = %s", (harbour["id"],))
    home = vault_dir / HARBOUR
    home.write_text(home.read_text().replace("Cutover planned for October.", "Cutover moved to November."))
    report = obsidian.import_vault(conn, vault_dir)
    assert _obj(conn, "Harbour cutover")["body"].count("October") == 1
    assert HARBOUR in report["kept_your_change"]


def test_a_changed_file_updates_what_talos_web_did_not_touch(conn, vault_dir):
    obsidian.import_vault(conn, vault_dir)
    home = vault_dir / HARBOUR
    home.write_text(home.read_text().replace("Cutover planned for October.", "Cutover moved to November."))
    state = vault_dir / "Operations/Systems/Nimbus NAS/State/current.md"
    state.write_text(state.read_text().replace("health: degraded", "health: healthy"))
    report = obsidian.import_vault(conn, vault_dir)
    assert "November" in _obj(conn, "Harbour Migration")["body"]
    assert _obj(conn, "Nimbus NAS")["attrs"]["state"]["health"] == "healthy"
    assert report["counts"]["binder"]["updated"] == 2 and report["counts"]["binder"]["unchanged"] == 4


def test_a_dry_run_writes_nothing_and_reports_the_same(conn, vault_dir, cutover_mail):
    before = _snapshot(conn)
    dry = obsidian.import_vault(conn, vault_dir, dry_run=True)
    assert _snapshot(conn) == before
    assert conn.execute("select count(*) as n from object").fetchone()["n"] == 0
    real = obsidian.import_vault(conn, vault_dir)
    assert dry["dry_run"] and not real["dry_run"]
    for key in ("counts", "kinds", "skipped", "placeholders", "status_mapped", "warnings"):
        assert dry[key] == real[key], key
    assert [x["file"] for x in dry["links"]["linked"]] == [x["file"] for x in real["links"]["linked"]]


def test_the_vault_is_only_read(conn, vault_dir):
    def state():
        return {p: (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
                for p in sorted(vault_dir.rglob("*")) if p.is_file()}
    before = state()
    obsidian.import_vault(conn, vault_dir)
    assert state() == before


def test_the_cli_prints_the_report_and_writes_it_to_the_logs(conn, database, vault_dir, tmp_path, monkeypatch, capsys):
    import json

    from talos import cli
    monkeypatch.setenv("TALOS_DSN", database)
    monkeypatch.setenv("TALOS_HOME", str(tmp_path / "home"))
    cli.main(["vault", "import", str(vault_dir), "--dry-run"])
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "binders" in out and "todo -> next" in out
    [log] = (tmp_path / "home" / "logs").glob("vault-import-*.json")
    assert json.loads(log.read_text())["counts"]["binder"]["created"] == 6
    assert conn.execute("select count(*) as n from object").fetchone()["n"] == 0
