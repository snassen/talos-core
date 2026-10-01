"""The enrichment pre-pass (step A): reasons, patterns, sender profiles, origin by rules. Invented mail only."""

import json
from datetime import datetime, timedelta, timezone

import mailfactory as mf
import pytest

from talos import cli, enrich, events, mime, reparse, rules
from talos.ingest import Location

ME = mf.ME
NOW = datetime.now(timezone.utc)
_keys = iter(range(1, 1_000_000))


def mail(ingestor, *, thread=None, days_ago=1, **kw) -> int:
    n = next(_keys)
    kw.setdefault("msgid", f"<e{n}@test.invalid>")
    kw.setdefault("date", NOW - timedelta(days=days_ago) + timedelta(seconds=n))  # later mail, later date
    return ingestor.ingest("gmail", mf.make(**kw), Location("[all]", f"e{n}", provider_thread_id=thread)).message_id


def origin(conn, mid):
    """(value, signal) of the message's own active origin, or None."""
    row = conn.execute("select value, source_ref from assignment where entity_id = %s and dimension_id = 'origin'"
                       " and status = 'active'", (mid,)).fetchone()
    if not row:
        return None
    signal, _, version = row["source_ref"].removeprefix(enrich.PREFIX).partition("@")
    assert int(version) == enrich.SIGNALS[signal]  # every value carries its signal's current version
    return row["value"], signal


def he_writes_to(ingestor, who, *, thread, subject="Hej", **kw) -> int:
    return mail(ingestor, frm=ME, to=who, subject=subject, body="Tack, jag återkommer.", thread=thread, **kw)


# ---------------------------------------------------------------- skeletons and pattern keys

@pytest.mark.parametrize("subject, skeleton", [
    ("SV: VB: Faktura 2026-09 från Telia", "faktura # från"),
    ("Sv: Re: Möte om brandväggen på fredag", "möte om brandväggen"),
    ("VB: Kvitto för köp 19/9 hos Nordvik", "kvitto för köp"),
    ("Ärende: Åtgärd krävs!", "ärende åtgärd krävs"),
    ("Din kod är 482913", "din kod är"),
    ("Re: Re[2]: Invoice INV-2026-0042 is due", "invoice # is"),
    ("Fwd: [Success] Backup job NAS01 (3 VMs)", "success backup job"),
    ("FW:RE : Order 123 456 confirmed", "order # confirmed"),
    ("AW: Ihre Bestellung #12345 ist unterwegs", "ihre bestellung #"),
    ("Reminder: the meeting moved", "reminder the meeting"),
    ("Re:", ""),
    ("", ""),
    (None, ""),
])
def test_the_subject_skeleton_strips_prefixes_masks_numbers_and_keeps_three_words(conn, subject, skeleton):
    assert conn.execute("select subject_skeleton(%s) as s", (subject,)).fetchone()["s"] == skeleton


def test_the_pattern_key_is_the_lower_cased_sender_and_the_skeleton(conn):
    key = conn.execute("select subject_pattern_key('Faktura@Telia.SE', 'SV: Din faktura 0923 är här') as k").fetchone()
    assert key["k"] == "faktura@telia.se|din faktura #"
    same = conn.execute("select subject_pattern_key('faktura@telia.se', 'Din faktura 1023 är här') as k").fetchone()
    assert same["k"] == key["k"]  # next month's invoice is the same template


def test_ingest_stores_each_mails_pattern_and_the_prepass_counts_them(conn, ingestor):
    ids = [mail(ingestor, frm="Telia <faktura@telia.se>", subject=f"Din faktura {m} är här", thread=f"f{m}")
           for m in ("0723", "0823", "0923")]
    other = mail(ingestor, frm="Telia <faktura@telia.se>", subject="Välkommen som kund")
    rows = conn.execute("select message_id, pattern_key from message_pattern order by message_id").fetchall()
    assert [r["pattern_key"] for r in rows] == ["faktura@telia.se|din faktura #"] * 3 + ["faktura@telia.se|välkommen som kund"]
    conn.execute("delete from message_pattern where message_id = %s", (other,))  # as if ingested before this step
    assert enrich.patterns(conn) == {"keys_written": 1, "patterns": 2, "patterns_written": 2, "patterns_removed": 0}
    p = conn.execute("select * from subject_pattern where skeleton = 'din faktura #'").fetchone()
    assert (p["message_count"], p["thread_count"], p["accounts"]) == (3, 3, ["gmail"])
    assert p["sample_ids"] == ids  # first, middle and last


# ---------------------------------------------------------------- automated reasons

REASON_CASES = {
    "person": {},
    "auto": {"Auto-Submitted": "auto-generated"},
    "auto-no": {"Auto-Submitted": "no"},
    "bulk": {"Precedence": "bulk"},
    "list-id": {"List-Id": "<styrelsen.nordvik.se>"},
    "unsubscribe": {"List-Unsubscribe": "<https://nordvik.se/av>"},
    "bounce": {"Return-Path": "<>"},
}


def test_the_reasons_recomputed_from_stored_headers_equal_the_parsers(conn, ingestor):
    ids = [mail(ingestor, subject=name, headers=h) for name, h in REASON_CASES.items()]
    ids.append(mail(ingestor, frm="Tjänst <no-reply+42@tjanst.se>", subject="noreply"))
    ids.append(mail(ingestor, frm="Posten <notifications@posten.se>", subject="both",
                    headers={"Precedence": "list", "List-Unsubscribe": "<mailto:av@posten.se>"}))
    stored = {r["id"]: r["headers"]["automated"] for r in conn.execute("select id, headers from message")}
    assert stored[ids[0]] == [] and stored[ids[-1]] == ["precedence", "list", "noreply-sender"]
    conn.execute("update message set headers = headers - 'automated'")
    assert enrich.backfill_reasons(conn, batch=3) == len(ids)
    assert {r["id"]: r["headers"]["automated"] for r in conn.execute("select id, headers from message")} == stored
    assert enrich.backfill_reasons(conn) == 0  # nothing left: a rerun reads nothing again


def test_a_rule_can_match_an_automated_reason(conn, ingestor):
    mail(ingestor, frm="Tjänst <noreply@tjanst.se>", subject="Din kod")
    mail(ingestor, subject="Hej", headers={"Precedence": "bulk"})
    assert rules.preview(conn, [{"field": "automated_reason", "op": "is", "value": "noreply-sender"}])["count"] == 1
    assert rules.preview(conn, [{"field": "automated_reason", "op": "is_set"}])["count"] == 2


def test_reparse_refreshes_the_reasons_and_the_pattern_and_leaves_teams_rows_alone(conn, ingestor, vault):
    mid = mail(ingestor, frm="Tjänst <noreply@tjanst.se>", subject="Din kod 1234")
    conn.execute("update message set parser_version = 1, headers = headers - 'automated', is_automated = false"
                 " where id = %s", (mid,))
    conn.execute("delete from message_pattern where message_id = %s", (mid,))
    chat = mail(ingestor, subject="Lunch?")
    conn.execute("update message set medium = 'teams_chat', parser_version = 1 where id = %s", (chat,))
    conn.commit()
    assert reparse.outdated(conn) == 1  # the Teams row is not the mail parser's
    assert reparse.reparse(conn, vault) == {"reparsed": 1, "failed": 0, "remaining": 0}
    m = conn.execute("select headers, is_automated from message where id = %s", (mid,)).fetchone()
    assert m["headers"]["automated"] == ["noreply-sender"] and m["is_automated"]
    assert conn.execute("select pattern_key from message_pattern where message_id = %s",
                        (mid,)).fetchone()["pattern_key"] == "noreply@tjanst.se|din kod #"


# ---------------------------------------------------------------- sender profiles

def test_a_sender_profile_counts_mail_replies_and_machine_shares(conn, ingestor):
    oskar = "Oskar Nyström <oskar@nordvik.se>"
    mail(ingestor, frm=oskar, subject="Offert", thread="t1", days_ago=5)
    he_writes_to(ingestor, oskar, thread="t1", subject="Re: Offert", days_ago=4)
    he_writes_to(ingestor, oskar, thread="t9", subject="Lunch", days_ago=3)
    mail(ingestor, frm=oskar, subject="Nyhetsbrev", thread="t2", days_ago=2,
         headers={"List-Unsubscribe": "<https://nordvik.se/av>"})
    enrich.patterns(conn)
    assert enrich.profiles(conn)["profiles"] == 1  # the owner's own address has no profile: it sends no incoming mail
    p = conn.execute("select * from sender_profile").fetchone()
    assert (p["account_id"], p["address"], p["message_count"]) == ("gmail", "oskar@nordvik.se", 2)
    assert (p["written_to"], p["written_to_all"], p["threads"], p["replied_threads"]) == (2, 2, 2, 1)
    assert (p["list_unsubscribe_share"], p["automated_share"], p["event_share"], p["templates"]) == (0.5, 0.5, 0, 2)


# ---------------------------------------------------------------- origin: one case per value

def _archive(conn, ingestor) -> dict:
    """One message per origin signal, and the ambiguous ones that must stay empty."""
    ids = {}
    oskar, helena = "Oskar Nyström <oskar@nordvik.se>", "Helena Roos <helena.roos@nordvik.se>"
    # alert: an event fired, a failed backup
    ids["alert"] = mail(ingestor, frm="Veeam <veeam@backup.nordvik.se>", subject="[Failed] Backup job NAS01",
                        headers={"Auto-Submitted": "auto-generated"})
    # system_report: a backup event that went well
    ids["system_report"] = mail(ingestor, frm="Veeam <veeam@backup.nordvik.se>", subject="[Success] Backup job NAS02",
                                headers={"Auto-Submitted": "auto-generated"})
    # notification: the owner's rule types it a notification
    ids["notification"] = mail(ingestor, frm="GitHub <noreply@github.com>", subject="Ny följare")
    # transactional: the owner's rule types it a receipt
    ids["transactional"] = mail(ingestor, frm="Klarna <kvitto@klarna.com>", subject="Ditt kvitto från Nordvik")
    # marketing: List-Unsubscribe from a bulk mailer the owner never wrote to
    ids["marketing"] = mail(ingestor, frm="Nordvik <nyheter@nordvik.se>", subject="Höstens nyheter",
                            headers={"List-Unsubscribe": "<https://nordvik.se/av>", "Precedence": "bulk"})
    ids["marketing_esp"] = mail(ingestor, frm="Butiken <hej@butiken.se>", subject="Veckans erbjudanden",
                                headers={"List-Unsubscribe": "<https://butiken.se/av>",
                                         "X-Mailer": "Mailchimp Mailer - **CID1234**"})
    # auto_reply: an automatic reply (out of office)
    ids["auto_reply"] = mail(ingestor, frm="Mikael <mikael@nordvik.se>", subject="Automatiskt svar: Offert",
                             headers={"Auto-Submitted": "auto-replied"})
    # mail_system: a bounce, and a quarantine digest
    ids["bounce"] = mail(ingestor, frm="Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
                         subject="Delivery Status Notification (Failure)", headers={"Auto-Submitted": "auto-replied"})
    ids["bounce_ndr"] = mail(ingestor, frm="Microsoft Outlook <microsoftexchange1234@nordvik.onmicrosoft.com>",
                             subject="Olevererbart: Offert", headers={"Auto-Submitted": "auto-replied"})
    ids["quarantine"] = mail(ingestor, frm="Microsoft 365 <quarantine@messaging.microsoft.com>",
                             subject="Microsoft 365 säkerhet: Du har meddelanden i karantän")
    # list: a List-Id that three parents write to
    for who in ("Anna <anna@example.se>", "Bo <bo@example.se>", "Cia <cia@example.se>"):
        ids.setdefault("list", mail(ingestor, frm=who, subject=f"Klassfest ({who[:3]})",
                                    headers={"List-Id": "<foraldrar.klass3.example.se>"}))
    # person: a correspondent in a thread the owner replied in
    ids["person"] = mail(ingestor, frm=oskar, subject="Offert", thread="t1", days_ago=3)
    he_writes_to(ingestor, oskar, thread="t1", subject="Re: Offert", days_ago=2)
    # person: someone the owner often writes to, in a thread of their own not answered yet
    for i in range(2):
        mail(ingestor, frm=helena, subject=f"Planering {'ab'[i]}", thread=f"h{i}", days_ago=10 + i)
        he_writes_to(ingestor, helena, thread=f"h{i}", subject=f"Re: Planering {'ab'[i]}", days_ago=9 + i)
    ids["correspondent"] = mail(ingestor, frm=helena, subject="Kan du titta på budgeten?", thread="h9")
    # person via a system: a person's name + "via", and a Sender header on a known correspondent's mail
    ids["via_name"] = mail(ingestor, frm='"Anna Berg via Blocket" <noreply@blocket.se>',
                           subject="Nytt meddelande om din annons")
    ids["sender_header"] = mail(ingestor, frm=oskar, subject="Inbjudan: Workshop", thread="t7",
                                headers={"Sender": "calendar@bokning.example.se"})

    # ---- ambiguous: these must stay empty
    ids["unknown_person"] = mail(ingestor, frm="Okänd Person <okand@example.org>", subject="En fråga")
    ids["noreply_only"] = mail(ingestor, frm="Tjänst <noreply@tjanst.se>", subject="Din kod är 1234")
    ids["unsubscribe_only"] = mail(ingestor, frm="Forum <forum@forum.example>", subject="Nya inlägg",
                                   headers={"List-Unsubscribe": "<https://forum.example/av>"})
    ids["bulk_receipt_words"] = mail(ingestor, frm="Nordvik <order@nordvik.se>", subject="Din order är skickad",
                                     headers={"List-Unsubscribe": "<https://nordvik.se/av>", "Precedence": "bulk"})
    ids["via_with_unsubscribe"] = mail(ingestor, frm='"Anna Berg via Patreon" <bingo@patreon.com>',
                                       subject="Nytt inlägg", headers={"List-Unsubscribe": "<https://patreon.com/av>"})
    ids["via_not_a_person"] = mail(ingestor, frm='"Simployer via Winningtemp" <noreply@winningtemp.com>',
                                   subject="Veckans enkät")
    # a correspondent's mail that the owner's rule types a receipt: person and machine disagree
    ids["conflict"] = mail(ingestor, frm=oskar, subject="Kvitto på lunchen", thread="t1", days_ago=1)
    # a sender the owner corresponds with, whose templated mail spans many threads (a monitoring system)
    drift = "Drift <drift@nordvik.se>"
    for i in range(2):
        mail(ingestor, frm=drift, subject=f"Fråga om server {i}", thread=f"d{i}", days_ago=20 + i)
        he_writes_to(ingestor, drift, thread=f"d{i}", subject="Re: Fråga", days_ago=19 + i)
    for i in range(3):
        ids.setdefault("templated", mail(ingestor, frm=drift, subject=f"Server {i} down", thread=f"s{i}"))
    # a List-Id where one sender writes most of the mail: a newsletter, not a list
    for who, n in (("Nyhetsbrev <brev@klubb.example>", 5), ("Ordf <ordf@klubb.example>", 1),
                   ("Kassör <kassor@klubb.example>", 1)):
        for i in range(n):
            ids.setdefault("one_voice_list", mail(ingestor, frm=who, subject=f"Klubbnytt {who[:4]} {i}",
                                                  headers={"List-Id": "<klubb.example>"}))

    rules.save(conn, rules.Rule("rc", "Klarna is a receipt", [{"field": "from_domain", "op": "is", "value": "klarna.com"}],
                                {"dimension": "type", "value": "receipt"}))
    rules.save(conn, rules.Rule("gh", "GitHub notifies", [{"field": "from_domain", "op": "is", "value": "github.com"}],
                                {"dimension": "type", "value": "notification"}))
    rules.save(conn, rules.Rule("rk", "Kvitto is a receipt", [{"field": "subject", "op": "starts_with", "value": "Kvitto"}],
                                {"dimension": "type", "value": "receipt"}))
    rules.run_all(conn)
    events.run(conn)
    return ids


EXPECTED = {
    "alert": ("alert", "event"),
    "system_report": ("system_report", "event"),
    "notification": ("notification", "type"),
    "transactional": ("transactional", "type"),
    "marketing": ("marketing", "bulk_mailer"),
    "marketing_esp": ("marketing", "bulk_mailer"),
    "auto_reply": ("auto_reply", "auto_reply"),
    "bounce": ("mail_system", "bounce"),
    "bounce_ndr": ("mail_system", "bounce"),
    "quarantine": ("mail_system", "quarantine"),
    "list": ("list", "mailing_list"),
    "person": ("person", "thread_reply"),
    "correspondent": ("person", "correspondent"),
    "via_name": ("person_via_system", "via_name"),
    "sender_header": ("person_via_system", "sender_header"),
}
AMBIGUOUS = ["unknown_person", "noreply_only", "unsubscribe_only", "bulk_receipt_words", "via_with_unsubscribe",
             "via_not_a_person", "conflict", "templated", "one_voice_list"]


def test_the_prepass_decides_origin_only_where_the_signals_agree(conn, ingestor):
    ids = _archive(conn, ingestor)
    res = enrich.prepass(conn)
    got = {name: origin(conn, ids[name]) for name in (*EXPECTED, *AMBIGUOUS)}
    assert {k: got[k] for k in EXPECTED} == EXPECTED
    assert {k: got[k] for k in AMBIGUOUS} == dict.fromkeys(AMBIGUOUS)
    assert set(v for v, _ in EXPECTED.values()) == set(enrich.ORIGINS) - {"spam"}  # every value has a case (spam: Jev's)
    assert res["origin"]["by_signal"]["conflict"] == 1
    assert not conn.execute("select 1 from assignment where dimension_id = 'origin' and entity_id in"
                            " (select id from message where direction <> 'in')").fetchone()  # incoming only
    row = conn.execute("select * from assignment where entity_id = %s and dimension_id = 'origin'",
                       (ids["alert"],)).fetchone()
    assert (row["source_kind"], row["source_ref"], row["evidence"]) == (
        "rule", "prepass:origin.event@2", {"signal": "event", "version": 2})


def test_teams_messages_are_people_unless_an_app_posted_them(conn, ingestor):
    user, app = mail(ingestor, subject="Lunch?"), mail(ingestor, subject="Build failed")
    conn.execute("update message set medium = 'teams_chat' where id = any(%s)", ([user, app],))
    conn.execute("update message set is_automated = true where id = %s", (app,))
    enrich.prepass(conn)
    assert (origin(conn, user), origin(conn, app)) == (("person", "teams_user"), ("notification", "teams_app"))


@pytest.mark.usefixtures("taxonomy_loaded")
@pytest.mark.parametrize("type_, want", [
    ("security_event", "alert"), ("alarm", "alert"), ("incident", "alert"), ("backup", "alert"),
    ("security", "alert"), ("report", "system_report"), ("receipt", "transactional"), ("shipping", "transactional"),
    ("subscription", "transactional"), ("statement", "transactional"), ("payment", "transactional"),
    ("sign_in", "transactional"), ("password", "transactional"), ("newsletter", "marketing"),
    ("event_webinar", "marketing"), ("survey", "marketing"), ("calendar_response", "auto_reply"),
    ("notification", "notification"), ("conversation", None), ("invitation", None),
])
def test_a_type_gives_the_origin_of_the_taxonomy(conn, ingestor, type_, want):
    mid = mail(ingestor, frm="Någon <nagon@example.org>", subject="Något")
    rules.assign(conn, [mid], "type", type_)
    enrich.prepass(conn)
    assert origin(conn, mid) == ((want, "type") if want else None)


def test_every_type_the_prepass_maps_is_in_the_taxonomy_and_every_origin_it_gives_too():
    from talos import taxonomy
    dims = {k: [v["value"] for v in d["values"]] for k, d in taxonomy.read().items()}
    assert tuple(dims["origin"]) == enrich.ORIGINS
    assert set(enrich.TYPE_ORIGINS) <= set(dims["origin"])
    assert {t for ts in enrich.TYPE_ORIGINS.values() for t in ts} <= set(dims["type"])


def test_a_backup_that_went_well_is_a_report_and_anything_else_from_a_system_an_alert(conn, ingestor):
    veeam = "Veeam <veeam@backup.nordvik.se>"
    auto = {"Auto-Submitted": "auto-generated"}
    ok = mail(ingestor, frm=veeam, subject="[Success] Backup job A", headers=auto)
    warn = mail(ingestor, frm=veeam, subject="[Warning] Backup job B", headers=auto)
    info = mail(ingestor, frm=veeam, subject="Backup server backup5 down", headers=auto)  # no status word: info
    alarm_ok = mail(ingestor, frm="Larmbolaget <larm@larmbolaget.example>", subject="Larm återställt, ok")
    events.run(conn)
    status = {r["message_id"]: (r["kind"], r["status"]) for r in conn.execute("select message_id, kind, status from event")}
    assert status[ok] == ("backup", "ok") and status[warn] == ("backup", "warning") and status[info] == ("backup", "info")
    assert status[alarm_ok] == ("alarm", "ok")
    enrich.prepass(conn)
    assert origin(conn, ok) == ("system_report", "event")
    assert [origin(conn, i) for i in (warn, info, alarm_ok)] == [("alert", "event")] * 3


def test_a_signal_with_a_new_version_replaces_its_old_values_on_the_next_run(conn, ingestor):
    ids = _archive(conn, ingestor)
    enrich.prepass(conn)
    # what version 1 of the bounce signal wrote, before the taxonomy
    conn.execute("delete from assignment where entity_id = %s and dimension_id = 'origin'", (ids["bounce"],))
    conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, confidence)"
                 " values (%s, 'origin', 'notification', 'rule', 'prepass:origin.bounce@1', 1.0)", (ids["bounce"],))
    res = enrich.prepass(conn)
    assert (res["origin"]["added"], res["origin"]["removed"]) == (1, 1)
    assert origin(conn, ids["bounce"]) == ("mail_system", "bounce")
    assert not conn.execute("select 1 from assignment where source_ref = 'prepass:origin.bounce@1'").fetchone()


def test_origin_is_a_closed_one_value_dimension_and_type_is_closed_too(conn):
    dims = {r["id"]: r for r in conn.execute("select id, cardinality, allowed from dimension")}
    assert dims["origin"]["cardinality"] == "one" and set(dims["origin"]["allowed"]) == set(enrich.ORIGINS)
    assert {"receipt", "invoice", "newsletter", "notification", "alert", "backup", "invitation"} <= set(dims["type"]["allowed"])
    with pytest.raises(rules.RuleError):
        rules.assign(conn, [1], "origin", "robot")


# ---------------------------------------------------------------- living beside the rule table

def test_a_rules_run_keeps_the_prepass_values_and_the_prepass_keeps_the_rules(conn, ingestor):
    ids = _archive(conn, ingestor)
    enrich.prepass(conn)
    before = conn.execute("select id, entity_id, value, source_ref from assignment where source_ref like 'prepass:%'"
                          " order by id").fetchall()
    assert before
    rules.run_all(conn)
    assert conn.execute("select id, entity_id, value, source_ref from assignment where source_ref like 'prepass:%'"
                        " order by id").fetchall() == before
    typed = conn.execute("select id, entity_id, source_ref from assignment where source_ref like 'rule:%'"
                         " order by id").fetchall()
    enrich.prepass(conn)
    assert conn.execute("select id, entity_id, source_ref from assignment where source_ref like 'rule:%'"
                        " order by id").fetchall() == typed
    assert origin(conn, ids["transactional"]) == ("transactional", "type")


def test_a_rule_that_sets_origin_wins_over_the_prepass_whichever_runs_first(conn, ingestor):
    ids = _archive(conn, ingestor)
    enrich.prepass(conn)
    assert origin(conn, ids["marketing"]) == ("marketing", "bulk_mailer")
    rules.save(conn, rules.Rule("nv", "Nordvik's mail is a notification",
                                [{"field": "from_address", "op": "is", "value": "nyheter@nordvik.se"}],
                                {"dimension": "origin", "value": "notification"}))
    rules.run_all(conn)  # the pre-pass's value does not block the rule (first wins counts only rule-table rules)
    enrich.prepass(conn)  # and the pre-pass steps aside for the rule
    values = conn.execute("select value, source_ref from assignment where entity_id = %s and dimension_id = 'origin'",
                          (ids["marketing"],)).fetchall()
    assert values == [{"value": "notification", "source_ref": "rule:nv@1"}]
    eff = conn.execute("select value from effective_message_assignment where message_id = %s"
                       " and dimension_id = 'origin'", (ids["marketing"],)).fetchone()
    assert eff["value"] == "notification"


def test_a_human_origin_beats_the_prepass(conn, ingestor):
    ids = _archive(conn, ingestor)
    enrich.prepass(conn)
    rules.assign(conn, [ids["marketing"]], "origin", "notification")
    enrich.prepass(conn)
    eff = conn.execute("select value, source_kind from effective_message_assignment where message_id = %s"
                       " and dimension_id = 'origin'", (ids["marketing"],)).fetchone()
    assert (eff["value"], eff["source_kind"]) == ("notification", "human")


# ---------------------------------------------------------------- idempotence and the dry run

def test_the_prepass_is_idempotent(conn, ingestor):
    _archive(conn, ingestor)
    first = enrich.prepass(conn)
    assert first["origin"]["added"] > 0
    snapshot = conn.execute("select id, entity_id, value, source_ref, created_at from assignment order by id").fetchall()
    profiles = conn.execute("select * from sender_profile order by account_id, address").fetchall()
    second = enrich.prepass(conn)
    assert (second["reasons"], second["patterns"]["keys_written"], second["patterns"]["patterns_written"],
            second["profiles"]["written"], second["origin"]["added"], second["origin"]["removed"]) == (0, 0, 0, 0, 0, 0)
    assert second["origin"]["by_signal"] == first["origin"]["by_signal"]
    assert conn.execute("select id, entity_id, value, source_ref, created_at from assignment"
                        " order by id").fetchall() == snapshot
    assert conn.execute("select * from sender_profile order by account_id, address").fetchall() == profiles


def test_a_changed_signal_moves_the_value_on_the_next_run(conn, ingestor):
    ids = _archive(conn, ingestor)
    enrich.prepass(conn)
    assert origin(conn, ids["unknown_person"]) is None
    he_writes_to(ingestor, "Okänd Person <okand@example.org>", thread=None, subject="Svar")
    # the owner's reply lands in the same thread
    tid = conn.execute("select thread_id from message where id = %s", (ids["unknown_person"],)).fetchone()["thread_id"]
    conn.execute("update message set thread_id = %s where id = (select max(id) from message)", (tid,))
    res = enrich.prepass(conn)
    assert origin(conn, ids["unknown_person"]) == ("person", "thread_reply") and res["origin"]["added"] == 1


def test_a_dry_run_reports_the_counts_and_keeps_nothing(conn, ingestor):
    _archive(conn, ingestor)
    conn.execute("update message set headers = headers - 'automated' where subject = 'Höstens nyheter'")
    conn.commit()
    res = enrich.prepass(conn, dry_run=True)
    assert res["dry_run"] and res["reasons"] == 1 and res["origin"]["added"] > 0
    assert not conn.execute("select 1 from assignment where source_ref like 'prepass:%'").fetchone()
    assert not conn.execute("select 1 from sender_profile").fetchone()
    assert not conn.execute("select 1 from subject_pattern").fetchone()
    assert not conn.execute("select 1 from message where subject = 'Höstens nyheter'"
                            " and headers ? 'automated'").fetchone()


# ---------------------------------------------------------------- the report and the command

def test_the_report_counts_coverage_patterns_and_jev_cases(conn, ingestor, tmp_path):
    ids = _archive(conn, ingestor)
    enrich.prepass(conn)
    rep = enrich.report(conn)
    incoming = conn.execute("select count(*) n from message where direction = 'in'").fetchone()["n"]
    assert rep["email"]["incoming"] == incoming
    assert rep["email"]["decided"] + rep["email"]["undecided"] == incoming
    assert rep["email"]["decided"] == conn.execute("select count(*) n from assignment where dimension_id = 'origin'"
                                                   " and status = 'active'").fetchone()["n"]
    assert rep["email"]["by_value"]["person"] >= 2 and rep["coverage"][0]["account_id"] == "gmail"
    # machine mail by the plan's definition: automated, an event, or a noise type
    assert rep["patterns"]["machine_mail"] == conn.execute(
        "select count(*) n from message where direction = 'in' and is_automated").fetchone()["n"]
    jev = rep["jev_estimate"]
    assert jev["machine_all"]["messages"] == rep["patterns"]["machine_mail"]
    assert jev["person_threads"]["threads"] > 0 and jev["teams_windows"]["windows"] == 0
    assert jev["total_plan_method"] == (jev["machine_all"]["cases"] + jev["person_threads"]["cases"]
                                        + jev["teams_windows"]["capped"])
    path = enrich.write_report(rep, tmp_path / "logs")
    assert json.loads(path.read_text())["email"]["incoming"] == incoming
    assert "Estimated Jev cases" in enrich.format_report(rep)
    assert ids  # (the archive is used)


def test_teams_windows_split_at_two_hour_gaps(conn, ingestor):
    t0 = NOW - timedelta(days=1)
    chat = []
    for minutes in (0, 30, 60, 300, 310):  # a 4-hour gap starts a second window
        n = next(_keys)
        chat.append(ingestor.ingest("gmail", mf.make(subject="Chatt", msgid=f"<c{n}@test.invalid>"),
                                    Location("[all]", f"c{n}", provider_thread_id="chat1",
                                             received_at=t0 + timedelta(minutes=minutes))).message_id)
    conn.execute("update message set medium = 'teams_chat' where id = any(%s)", (chat,))
    enrich.prepass(conn)
    tw = enrich.report(conn)["jev_estimate"]["teams_windows"]
    assert (tw["messages"], tw["windows"], tw["capped"]) == (5, 2, 2)


def test_talos_enrich_runs_from_the_command_line(conn, ingestor, vault, database, monkeypatch, capsys):
    _archive(conn, ingestor)
    conn.commit()
    monkeypatch.setenv("TALOS_DSN", database)
    monkeypatch.setenv("TALOS_HOME", str(vault.root.parent))
    monkeypatch.setattr(cli, "_logging", lambda settings, verbose: None)
    cli.main(["enrich", "prepass", "--dry-run"])
    out = capsys.readouterr().out
    assert '"dry_run": true' in out and "rolled back" in out
    assert not conn.execute("select 1 from assignment where source_ref like 'prepass:%'").fetchone()
    cli.main(["enrich", "prepass"])
    cli.main(["enrich", "report"])
    out = capsys.readouterr().out
    assert "Origin coverage" in out and "enrich-report-" in out
    assert list((vault.root.parent / "logs").glob("enrich-report-*.json"))
    assert mime.PARSER_VERSION  # (imported for the reparse test)


def test_sync_then_rules_runs_the_prepass_only_when_mail_was_added(conn, ingestor, vault, database, monkeypatch,
                                                                   capsys):
    from types import SimpleNamespace

    from talos import accounts
    from talos.sources import base
    added = {"n": 0}

    def fake_run(conn_, ingestor_, account_id, source, limit=None):
        for _ in range(added["n"]):
            mail(ingestor_, frm="Nordvik <nyheter@nordvik.se>", subject="Höstens nyheter",
                 headers={"List-Unsubscribe": "<https://nordvik.se/av>", "Precedence": "bulk"})
        return SimpleNamespace(seen=added["n"], added=added["n"], updated=0, gone=0, failed=0, notes=[])

    monkeypatch.setattr(base, "run", fake_run)
    monkeypatch.setattr(accounts, "source_for", lambda acct: None)
    monkeypatch.setenv("TALOS_DSN", database)
    monkeypatch.setenv("TALOS_HOME", str(vault.root.parent))
    monkeypatch.setattr(cli, "_logging", lambda settings, verbose: None)
    cli.main(["sync", "gmail", "--then-rules"])  # nothing new: no prepass
    assert "enrich:" not in capsys.readouterr().out
    assert not conn.execute("select 1 from sender_profile").fetchone()
    added["n"] = 1
    cli.main(["sync", "gmail", "--then-rules"])
    assert "enrich: origin +1 -0" in capsys.readouterr().out
    assert conn.execute("select value from assignment where dimension_id = 'origin'").fetchone()["value"] == "marketing"
