"""How the Changesets page groups and colours changesets, links undos, and says what happens next;
and the Overview's saved widget layout after a widget was removed."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import mailfactory as mf
import pytest
from starlette.testclient import TestClient

from talos import changesets
from talos.config import Settings
from talos.ingest import Location
from talos.web import app

APP_JS = Path(__file__).parent.parent / "src" / "talos" / "web" / "static" / "app.js"
CSS = Path(__file__).parent.parent / "src" / "talos" / "web" / "static" / "style.css"


def test_open_changesets_lead_and_finished_ones_go_to_history_with_undone_shown():
    rows = [{"id": 1, "status": "done", "undo_of": None}, {"id": 2, "status": "done", "undo_of": 1},
            {"id": 3, "status": "done", "undo_of": None}, {"id": 4, "status": "planned", "undo_of": 3},
            {"id": 5, "status": "planned", "undo_of": None}, {"id": 6, "status": "committed", "undo_of": None},
            {"id": 7, "status": "applying", "undo_of": None}, {"id": 8, "status": "failed", "undo_of": None},
            {"id": 9, "status": "cancelled", "undo_of": None}, {"id": 10, "status": "draft", "undo_of": None}]
    got = {r["id"]: r for r in changesets.lifecycle(rows)}
    assert {i: (r["shown"], r["group"]) for i, r in got.items()} == {
        1: ("undone", "history"), 2: ("done", "history"), 3: ("done", "history"), 4: ("planned", "open"),
        5: ("planned", "open"), 6: ("committed", "open"), 7: ("applying", "open"), 8: ("failed", "history"),
        9: ("cancelled", "history"), 10: ("draft", "open")}
    # An undo points back, and the undone one forward, whatever the undo's own status.
    assert (got[1]["undone_by"], got[2]["undo_of"]) == (2, 1)
    assert (got[3]["undone_by"], got[3]["undone_by_status"], got[4]["undo_of"]) == (4, "planned", 3)
    assert got[5]["undone_by"] is None and got[5]["undo_of"] is None


def _two_done_and_an_undo(conn, ingestor):
    mid = ingestor.ingest("gmail", mf.make(subject="Hej"), Location("[all]", "a", uidvalidity=1, uid=1,
                                                                   labels=["\\Inbox"])).message_id
    first = changesets.create(conn, "Label the test mail", "add_label", args={"label": "Talos/test"}, message_ids=[mid])
    changesets.plan(conn, first)
    conn.execute("update changeset set status = 'done', finished_at = now() where id = %s", (first,))
    conn.execute("update changeset_op set status = 'done' where changeset_id = %s", (first,))
    undo = changesets.undo(conn, first)
    conn.execute("update changeset set status = 'done', finished_at = now() where id = %s", (undo,))
    waiting = changesets.create(conn, "Flag it", "flag", message_ids=[mid])
    changesets.plan(conn, waiting)
    conn.commit()
    return first, undo, waiting


def test_the_api_lists_status_group_undo_links_and_op_counts(conn, ingestor, vault, database):
    first, undo, waiting = _two_done_and_an_undo(conn, ingestor)
    c = TestClient(app.create(Settings(home=vault.root.parent, dsn=database), allowed_hosts=["testserver"]))
    rows = {r["id"]: r for r in c.get("/api/changesets").json()}
    assert (rows[first]["shown"], rows[first]["group"], rows[first]["undone_by"]) == ("undone", "history", undo)
    assert (rows[undo]["shown"], rows[undo]["undo_of"]) == ("done", first)
    assert (rows[waiting]["shown"], rows[waiting]["group"], rows[waiting]["ops"]) == ("planned", "open", {"pending": 1})
    d = c.get(f"/api/changesets/{first}").json()
    assert (d["shown"], d["undone_by"], d["undone_by_status"], d["op_counts"]) == ("undone", undo, "done", {"done": 1})
    d = c.get(f"/api/changesets/{undo}").json()
    assert d["undo_of"] == first and d["group"] == "history"


def _grab(js, name):
    return re.search(rf"^function {name}\(.*?^}}\n", js, re.S | re.M).group(0)


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    return node


def test_the_pane_says_what_happens_next_in_plain_words(tmp_path):
    js = APP_JS.read_text(encoding="utf-8")
    code = _grab(js, "csWhen") + _grab(js, "csNext")
    cases = [
        {"id": 7, "shown": "planned", "op_counts": {"pending": 50}, "summary": {}},
        {"id": 7, "shown": "planned", "op_counts": {"pending": 2},
         "summary": {"dry_run": {"at": "2026-09-26T20:14:00", "ok": 1, "problems": 1}}},
        {"id": 8, "shown": "committed", "op_counts": {"pending": 10}, "committed_at": "2026-09-26T20:14:00"},
        {"id": 9, "shown": "done", "op_counts": {"done": 50, "skipped": 2}, "finished_at": "2026-09-26T20:14:00"},
        {"id": 9, "shown": "undone", "op_counts": {"done": 50}, "finished_at": "2026-09-26T20:14:00", "undone_by": 12},
        {"id": 12, "shown": "planned", "op_counts": {"pending": 50}, "undo_of": 9},
        {"id": 10, "shown": "failed", "op_counts": {"done": 3, "failed": 1}, "finished_at": "2026-09-26T20:14:00"},
        {"id": 11, "shown": "cancelled", "op_counts": {}, "finished_at": "2026-09-26T20:14:00"},
        {"id": 13, "shown": "applying", "op_counts": {"done": 4, "pending": 6}},
        {"id": 14, "shown": "planned", "op_counts": {"skipped": 3}, "summary": {}},
    ]
    (tmp_path / "t.js").write_text(code + f"console.log(JSON.stringify({json.dumps(cases)}.map(csNext)));\n", encoding="utf-8")
    out = json.loads(subprocess.run([_node(), str(tmp_path / "t.js")], capture_output=True, text=True, check=True).stdout)
    assert out[0].startswith("Planned: dry-run it, then commit to approve (talos changeset commit 7 --max 50)")
    assert out[1].startswith("Planned: the dry run 26 Sep 20:14 found 1 ready and 1 with a problem. Commit to approve")
    assert out[2] == "Committed 26 Sep 20:14: approved, not applied. Apply writes 10 messages to the server: talos changeset apply 8."
    assert out[3] == ("Done 26 Sep 20:14: 50 messages changed, 2 skipped."
                      " Undo creates a reversing changeset: talos changeset undo 9.")
    assert "undone by changeset #12" in out[4]
    assert out[5].endswith("It reverses changeset #9.")
    assert out[6].startswith("Failed 26 Sep 20:14: 3 messages changed, 1 failed")
    assert out[7] == "Cancelled 26 Sep 20:14: nothing more happens. Nothing was written."
    assert out[8].startswith("Applying: 4 of 10 done so far.")
    assert out[9].startswith("Planned, but nothing would change") and "talos changeset cancel 14" in out[9]


def test_every_status_has_its_colour_and_a_legend_entry():
    js, css = APP_JS.read_text(encoding="utf-8"), CSS.read_text(encoding="utf-8")
    legend = re.search(r"const CS_LEGEND = \[(.*?)\];", js).group(1)
    shown = re.findall(r"'(\w+)'", legend)
    assert shown == ["planned", "committed", "applying", "done", "failed", "cancelled", "undone"]
    colour = {"planned": "--accent", "committed": "--warning", "applying": "--warning", "done": "--good", "failed": "--critical"}
    for st in shown:
        rule = re.search(rf"\.pill\.cs-{st}[^{{]*\{{([^}}]*)\}}", css)
        assert rule, st
        if st in colour:
            assert colour[st] in rule.group(1) and "--series-" not in rule.group(1)   # never an account colour
    assert re.search(r"\.pill\.cs-applying \.cs-mark\{[^}]*animation:", css)
    assert "prefers-reduced-motion:reduce){.pill.cs-applying .cs-mark{animation:none}" in css


def test_a_saved_layout_that_still_names_the_removed_widget_is_tolerated(tmp_path):
    js = APP_JS.read_text(encoding="utf-8")
    widgets = re.search(r"^const WIDGETS = \[.*?^\];\n", js, re.S | re.M).group(0)
    assert "'daily'" not in widgets and "/api/importance/daily" not in js
    code = widgets + _grab(js, "widgetChoice") + _grab(js, "saveWidgetChoice")
    run = """
const store = {};
globalThis.localStorage = {getItem: k => k in store ? store[k] : null, setItem: (k, v) => { store[k] = v; }};
const out = [];
store['talos-widgets'] = JSON.stringify({daily: true, argus: false, people: true, archive: true});
const c = widgetChoice(); out.push(c);
saveWidgetChoice(c); out.push(JSON.parse(store['talos-widgets']));
store['talos-widgets'] = '["daily"]'; out.push(widgetChoice());
store['talos-widgets'] = 'not json'; out.push(widgetChoice());
console.log(JSON.stringify(out));
"""
    (tmp_path / "t.js").write_text(code + run, encoding="utf-8")
    got = json.loads(subprocess.run([_node(), str(tmp_path / "t.js")], capture_output=True, text=True, check=True).stdout)
    chosen, saved, from_list, from_junk = got
    assert "daily" not in chosen and "daily" not in saved
    # argus moved to Today (2026-09-28), so a saved choice that names it is read without it, like daily
    assert "argus" not in chosen and chosen["people"] is True and chosen["archive"] is True
    assert chosen["check"] is True                               # not in the saved choice: its default
    defaults = {w: v for w, v in from_junk.items()}
    assert from_list == defaults and "daily" not in defaults


def test_a_large_dry_run_reaches_the_page_as_its_problems_and_a_sample_and_the_list_leaves_it_out(conn, ingestor, vault, database):
    ids = [ingestor.ingest("gmail", mf.make(subject=f"Log {i}"), Location("[all]", str(i), labels=["\\\\Inbox"])).message_id
           for i in range(120)]
    cs = changesets.create(conn, "Many", "mark_read", message_ids=ids)
    changesets.plan(conn, cs)
    items = [{"message_id": m, "ok": n != 7, "problem": None if n != 7 else "gone", "would": [], "account": "gmail",
              "op": "mark_read", "args": {}, "subject": "x"} for n, m in enumerate(ids)]
    conn.execute("update changeset set summary = summary || %s where id = %s",
                 (json.dumps({"dry_run": {"at": "2026-09-28", "ok": 119, "problems": 1, "items": items}}), cs))
    conn.commit()
    c = TestClient(app.create(Settings(home=vault.root.parent, dsn=database), allowed_hosts=["testserver"]))
    dr = c.get(f"/api/changesets/{cs}").json()["summary"]["dry_run"]
    assert dr["items_total"] == 120 and len(dr["items"]) == 51 and dr["items"][0]["problem"] == "gone"
    row = next(r for r in c.get("/api/changesets").json() if r["id"] == cs)
    assert "items" not in row["summary"]["dry_run"] and row["summary"]["dry_run"]["ok"] == 119


def test_a_changeset_pane_shows_its_steps_apply_and_check():
    js = APP_JS.read_text()
    for fn in ("function csSteps(", "function csApplyCard(", "function csCheckCard("):
        assert fn in js
    assert "csSteps(cs)," in js and ".cs-steps" in CSS.read_text()
