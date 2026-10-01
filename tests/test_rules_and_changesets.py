import mailfactory as mf
import pytest
from psycopg.types.json import Jsonb

from talos import changesets, rules
from talos.ingest import Location


def seed(conn, ingestor):
    mails = [
        ("klarna", mf.make(frm="Klarna <noreply@klarna.com>", subject="Ditt kvitto från Hemköp",
                           headers={"Auto-Submitted": "auto-generated"}), ["\\Inbox", "Receipts"], ["seen"]),
        ("school", mf.make(frm="Skolan <info@skola.stockholm>", subject="Utvecklingssamtal vecka 40"),
         ["\\Inbox", "School"], []),
        ("oskar", mf.make(subject="Brandväggsfönster fredag?", attachments=[("plan.pdf", "application/pdf", mf.pdf("x"))]),
         ["\\Inbox"], []),
        ("backup", mf.make(frm="Veeam <noreply@backup.example>", subject="[Failed] Nightly job", body="Job failed"),
         ["Notifications"], ["seen"]),
    ]
    ids = {}
    for key, raw, labels, flags in mails:
        ids[key] = ingestor.ingest("gmail", raw, Location("[all]", key, uidvalidity=1, uid=len(ids) + 1,
                                                          provider_id=key, labels=labels, flags=flags)).message_id
    conn.commit()
    return ids


def matching(conn, conditions):
    return {r["subject"] for r in rules.preview(conn, conditions, sample=50)["sample"]}


def test_conditions_cover_sender_labels_attachments_headers_and_text(conn, ingestor):
    seed(conn, ingestor)
    assert matching(conn, [{"field": "from_domain", "op": "is", "value": "KLARNA.com"}]) == {"Ditt kvitto från Hemköp"}
    assert matching(conn, [{"field": "label", "op": "is", "value": "school"}]) == {"Utvecklingssamtal vecka 40"}
    assert matching(conn, [{"field": "attachment_type", "op": "is", "value": "application/pdf"}]) == \
        {"Brandväggsfönster fredag?"}
    assert matching(conn, [{"field": "header:auto_submitted", "op": "is_set"}]) == {"Ditt kvitto från Hemköp"}
    assert matching(conn, [{"field": "body", "op": "search", "value": "failed"}]) == {"[Failed] Nightly job"}
    assert matching(conn, [{"field": "is_automated", "op": "is_true"},
                           {"field": "subject", "op": "contains", "value": "fail"}]) == {"[Failed] Nightly job"}
    assert matching(conn, [{"field": "subject", "op": "contains", "value": "100%_"}]) == set()  # wildcards escaped


def test_bad_rules_are_refused_before_they_are_stored(conn):
    with pytest.raises(rules.RuleError):
        rules.compile_conditions([{"field": "nonsense", "op": "is", "value": 1}])
    with pytest.raises(rules.RuleError):
        rules.save(conn, rules.Rule("r", "r", [{"field": "subject", "op": "is", "value": "x"}],
                                    {"dimension": "nope", "value": "x"}))


def test_first_rule_wins_a_one_value_dimension_and_humans_are_never_overwritten(conn, ingestor):
    ids = seed(conn, ingestor)
    rules.save(conn, rules.Rule("receipts", "Receipts", [{"field": "label", "op": "is", "value": "Receipts"}],
                                {"dimension": "topic", "value": "Orders"}, priority=10))
    rules.save(conn, rules.Rule("groceries", "Hemköp", [{"field": "subject", "op": "contains", "value": "Hemköp"}],
                                {"dimension": "topic", "value": "Family/Groceries"}, priority=20))
    rules.save(conn, rules.Rule("auto", "Automated", [{"field": "is_automated", "op": "is_true"}],
                                {"dimension": "tag", "value": "machine"}, priority=30))
    rules.assign(conn, [ids["school"]], "topic", "School")
    counts = rules.run_all(conn)
    assert counts == {"receipts": 1, "groceries": 0, "auto": 2}
    rules.run_all(conn)  # re-running changes nothing
    eff = {(r["entity_id"], r["dimension_id"]): r for r in conn.execute("select * from effective_assignment")}
    assert eff[(ids["klarna"], "topic")]["value"] == "Orders"
    assert eff[(ids["klarna"], "topic")]["source_ref"] == "rule:receipts@1"
    assert eff[(ids["school"], "topic")]["value"] == "School"
    assert eff[(ids["school"], "topic")]["source_kind"] == "human"

    # A human decision on a rule-classified message wins, and survives the next rule run.
    rules.assign(conn, [ids["klarna"]], "topic", "Family/Groceries")
    rules.run_all(conn)
    eff = {(r["entity_id"], r["dimension_id"]): r["value"] for r in conn.execute("select * from effective_assignment")}
    assert eff[(ids["klarna"], "topic")] == "Family/Groceries"


def test_changing_a_rule_bumps_its_version_so_old_values_are_traceable(conn):
    r = rules.save(conn, rules.Rule("x", "x", [{"field": "subject", "op": "contains", "value": "a"}],
                                    {"dimension": "tag", "value": "a"}))
    assert r.version == 1
    r2 = rules.save(conn, rules.Rule("x", "x", [{"field": "subject", "op": "contains", "value": "b"}],
                                     {"dimension": "tag", "value": "a"}))
    assert r2.version == 2 and r2.ref == "rule:x@2"


class FakeExecutor:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def apply(self, op, args, targets):
        self.calls.append((op, dict(args), sorted(t.message_id for t in targets)))
        return {t.message_id: ("server said no" if t.message_id in self.fail else None) for t in targets}


def enable_writeback(conn, account="gmail"):
    conn.execute("update account set settings = settings || %s where id = %s",
                 (Jsonb({"writeback_enabled": True}), account))


def test_a_changeset_is_planned_against_the_mirror_and_skips_no_ops(conn, ingestor):
    ids = seed(conn, ingestor)
    cs = changesets.create(conn, "Arkivera allt maskinellt i inkorgen", "archive",
                           conditions=[{"field": "is_automated", "op": "is_true"}])
    summary = changesets.plan(conn, cs)
    conn.commit()
    # klarna is in the inbox; the backup mail is automated too but not in the inbox
    assert summary["will_change"] == 1 and summary["selected"] == 2
    assert summary["skipped_because"] == {"not in the inbox": 1}
    op = conn.execute("select * from changeset_op where message_id = %s", (ids["klarna"],)).fetchone()
    assert op["status"] == "pending" and op["inverse"] == {"op": "add_label", "args": {"label": "\\Inbox"}}


def test_nothing_is_applied_without_a_commit_and_enabled_writeback(conn, ingestor):
    seed(conn, ingestor)
    cs = changesets.create(conn, "Läst", "mark_read", conditions=[{"field": "label", "op": "is", "value": "\\Inbox"}])
    changesets.plan(conn, cs)
    with pytest.raises(changesets.ChangesetError):
        changesets.apply(conn, cs, {"gmail": FakeExecutor()})
    changesets.commit(conn, cs, max_ops=1000)
    with pytest.raises(changesets.WritebackDisabled):
        changesets.apply(conn, cs, {"gmail": FakeExecutor()})


def test_apply_batches_by_operation_records_each_result_and_can_be_undone(conn, ingestor):
    ids = seed(conn, ingestor)
    enable_writeback(conn)
    cs = changesets.create(conn, "Flagga skolan och Oskar", "flag", message_ids=[ids["school"], ids["oskar"]])
    changesets.plan(conn, cs)
    changesets.commit(conn, cs, max_ops=1000)
    ex = FakeExecutor(fail={ids["oskar"]})
    result = changesets.apply(conn, cs, {"gmail": ex}, chunk=10)
    assert result == {"done": 1, "failed": 1}
    assert ex.calls == [("flag", {}, sorted([ids["school"], ids["oskar"]]))]  # one call for the whole batch
    assert conn.execute("select status from changeset where id = %s", (cs,)).fetchone()["status"] == "failed"

    undo = changesets.undo(conn, cs)
    changesets.commit(conn, undo, max_ops=1000)
    ex2 = FakeExecutor()
    changesets.apply(conn, undo, {"gmail": ex2})
    assert ex2.calls == [("unflag", {}, [ids["school"]])]  # only what was actually done is reversed


def test_mail_that_left_the_server_is_skipped_not_retried(conn, ingestor):
    ids = seed(conn, ingestor)
    enable_writeback(conn)
    cs = changesets.create(conn, "Papperskorg", "trash", message_ids=[ids["school"]])
    changesets.plan(conn, cs)
    changesets.commit(conn, cs, max_ops=1000)
    conn.execute("update message_location set present = false where message_id = %s", (ids["school"],))
    ex = FakeExecutor()
    assert changesets.apply(conn, cs, {"gmail": ex}) == {"done": 0, "failed": 0}
    assert ex.calls == []
    assert conn.execute("select status, error from changeset_op").fetchone()["error"] == "no longer on the server"
