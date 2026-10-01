"""Jev on the answer key (enrichment step C): the questions, the records, masking, the client,
the routing policy, runs (resume, the cost guard, the dry run) and the report.

Never the real Jev: every request goes to a fake TypeSafe endpoint (httpx.MockTransport) that
answers in the real response shape. The key comes from a mocked Keychain. Invented mail only.
"""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from test_gold import _full, _items

from talos import cli, enrich, gold, jev, jev_gold, mask, personal, policy, secrets, taxonomy
from talos.policy import Policy

FILE = taxonomy.read()
DIMS = {f: FILE[f] for f in gold.FIELDS}
QUESTIONS = jev.build_questions(DIMS)                       # v2, the email set
QSETS = jev.question_sets(DIMS)                             # v2, {"email": ..., "teams": ...}
QUESTIONS_V1 = jev.build_questions(DIMS, template=1)
FAKE_KEY = "test-key-not-real-0123"


# ---------------------------------------------------------------- a fake Jev

def answer(questions: dict, pick: dict | None = None) -> dict:
    """Jev's answers for the questions: for a choice, `pick[name]` (value, p) or the first
    value at 0.9; the rest share what is left. For a statement, `pick[name]` or 0.1."""
    pick = pick or {}
    out = {}
    for name, q in questions.items():
        if q["type"] == "choice":
            values = list(q["criteria"])
            top, p = pick.get(name, (values[0], 0.9))
            rest = (1 - p) / (len(values) - 1)
            probs = {v: (p if v == top else rest) for v in values}
            out[name] = {"type": "choice", "choice": top, "confidence": p, "probabilities": probs}
        else:
            out[name] = {"type": "noul", "noul": pick.get(name, 0.1)}
    return out


class FakeJev:
    """A TypeSafe endpoint: checks the request shape, answers every question, counts usage.
    fail[item text marker] = list of statuses to return first (e.g. [429, 500])."""

    def __init__(self, pick=None, statuses=None, retry_after=None, input_tokens=5000):
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.pick = pick or {}
        self.statuses = list(statuses or [])
        self.retry_after = retry_after
        self.input_tokens = input_tokens
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and str(request.url) == jev.ENDPOINT
        body = json.loads(request.content)
        assert set(body) == {"state", "model", "questions"}
        self.requests.append(body)
        self.headers.append(dict(request.headers))
        if self.statuses:
            status = self.statuses.pop(0)
            headers = {"Retry-After": self.retry_after} if self.retry_after is not None else {}
            return httpx.Response(status, headers=headers, text="busy")
        pick = self.pick(body) if callable(self.pick) else self.pick
        return httpx.Response(200, json={"model": body["model"], "answers": answer(body["questions"], pick),
                                         "usage": {"input_tokens": self.input_tokens, "output_tokens": 400}})


@pytest.fixture
def keychain(monkeypatch):
    """The Keychain, mocked: records every read; the environment variable of the Codex runner
    is set to something else, to show it is never used."""
    reads = []

    def get(key):
        reads.append(key)
        if key != jev.KEY_NAME:
            raise secrets.MissingSecret(key)
        return FAKE_KEY
    monkeypatch.setattr(secrets, "get", get)
    monkeypatch.setenv("TYPESAFE_API_KEY", "from-the-environment")
    return reads


async def _no_sleep(_):
    return None


def client(fake: FakeJev, **kw) -> jev.JevClient:
    kw.setdefault("sleep", _no_sleep)
    return jev.JevClient(transport=fake.transport, **kw)


# ---------------------------------------------------------------- questions

def test_each_one_value_field_is_one_choice_whose_criteria_are_its_values_and_definitions():
    for f in ("origin", "type", "topic", "value"):
        q = QUESTIONS[f]
        assert q["type"] == "choice" and FILE[f]["description"] in q["instructions"]
        assert list(q["criteria"]) == [v["value"] for v in FILE[f]["values"]]  # every value id, in file order
        for v in FILE[f]["values"]:
            assert v["description"] in q["criteria"][v["value"]]
    # a label that says more than its id leads the criterion; one that repeats it does not
    assert QUESTIONS["type"]["criteria"]["sales_outreach"].startswith("Cold sales pitch: ")
    assert QUESTIONS["origin"]["criteria"]["person"] == "A human wrote it to you or to a group you are in, directly."


def test_templates_v1_still_ask_each_value_of_ask_and_route_as_a_statement_scored_on_its_own():
    for f in ("ask", "route"):
        names = [k for k in QUESTIONS_V1 if k.startswith(f + ":")]
        assert names == [f"{f}:{v['value']}" for v in FILE[f]["values"]]
        assert all(QUESTIONS_V1[n]["type"] == "noul" and set(QUESTIONS_V1[n]) == {"type", "instructions"}
                   for n in names)
    assert "you personally" in QUESTIONS_V1["ask:question"]["instructions"]
    assert QUESTIONS_V1["route:Security"]["instructions"].startswith(
        "This is work mail (not personal mail) that belongs to the work category Security: Security tooling")
    assert len(QUESTIONS_V1) == 4 + len(FILE["ask"]["values"]) + len(FILE["route"]["values"])
    assert jev.build_questions(DIMS, template=1, teams=True) == QUESTIONS_V1   # v1 has one set for both
    with pytest.raises(jev.JevError, match="question templates are one of 1, 2"):
        jev.build_questions(DIMS, template=3)


def test_the_question_version_changes_with_a_definition_or_the_recipient_and_only_then():
    v = jev.question_version(QUESTIONS)
    assert v == jev.question_version(jev.build_questions({f: FILE[f] for f in gold.FIELDS}))
    changed = json.loads(json.dumps({f: FILE[f] for f in gold.FIELDS}))
    changed["value"]["values"][0]["description"] = "Something else."
    assert jev.question_version(jev.build_questions(changed)) != v
    assert jev.question_version(QUESTIONS, recipient="Someone else.") != v


def test_the_questions_come_from_the_loaded_taxonomy(conn, taxonomy_loaded):
    assert jev.build_questions(jev.taxonomy_from_db(conn)) == QUESTIONS


def test_who_the_owner_is_goes_once_into_the_state_and_is_short(tmp_path, monkeypatch):
    body = jev.request_body({"unit": "message", "text": "Hej"}, QUESTIONS)
    assert body["state"]["recipient"] == jev.RECIPIENT and body["model"] == jev.MODEL
    assert jev.RECIPIENT == jev.GENERIC_RECIPIENT and len(jev.RECIPIENT) < 700   # the tests never read the owner's
    monkeypatch.setenv("TALOS_HOME", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "recipient.txt").write_text("A. Person, head of IT at Example AB.\n")
    assert personal.text("recipient.txt", jev.GENERIC_RECIPIENT) == "A. Person, head of IT at Example AB."
    assert not any("Example AB" in q["instructions"] for q in QUESTIONS.values())


def test_v1_answers_are_read_per_field_and_a_missing_or_malformed_answer_is_refused():
    dims = DIMS
    got = jev.read_answers(dims, answer(QUESTIONS_V1, {"origin": ("marketing", 0.8), "ask:action": 0.7}), template=1)
    assert got["origin"]["scores"]["marketing"] == 0.8 and not got["origin"]["many"]
    assert got["ask"]["many"] and got["ask"]["scores"]["action"] == 0.7
    assert set(got["route"]["scores"]) == {v["value"] for v in FILE["route"]["values"]}
    bad = answer(QUESTIONS_V1)
    del bad["route:Security"]
    with pytest.raises(jev.JevError, match="route:Security"):
        jev.read_answers(dims, bad, template=1)
    bad = answer(QUESTIONS_V1)
    bad["type"]["probabilities"]["invented"] = 0.0
    with pytest.raises(jev.JevError, match="unknown value"):
        jev.read_answers(dims, bad, template=1)


# ---------------------------------------------------------------- masking

@pytest.mark.parametrize("text, want", [
    ("Personnummer 19811218-9876.", "Personnummer [personnummer]."),
    ("pnr 811218-9876 och 811218+9876", "pnr [personnummer] och [personnummer]"),
    ("utan streck 8112189876", "utan streck [personnummer]"),              # passes the Luhn check
    ("samordningsnummer 811278-1234", "samordningsnummer [personnummer]"),  # day + 60
    ("kort 4111 1111 1111 1111 eller 4111-1111-1111-1111", "kort [card] eller [card]"),
    ("IBAN SE45 5000 0000 0583 9825 7466", "IBAN [iban]"),
    ("iban SE4550000000058398257466.", "iban [iban]."),
    ("ring 070-123 45 67 eller +46 70 123 45 67", "ring [phone] eller [phone]"),
    ("växel 031-12 34 56, 08-123 456 78, 0046701234567", "växel [phone], [phone], [phone]"),
    ("se https://portal.example.se/case/123?token=abc#top nu", "se https://portal.example.se/… nu"),
    ("www.blocket.se/annons/goteborg/cykel/1234", "www.blocket.se/…"),
])
def test_masking_replaces_ids_cards_ibans_phones_and_url_paths(text, want):
    assert mask.mask(text) == want


@pytest.mark.parametrize("text", [
    "Möte 2026-09-25 kl 08:30, 0,50 kr, version 1.2.3",   # dates, times, amounts, versions
    "Order 1234567890 och ärende REQ0012345",              # no valid date; a ticket id
    "Kort 1234 5678 9012 3456",                            # fails the Luhn check
    "Konto AB12 CDEF GHIJ KLMN",                           # fails mod 97
    "Utan fel 8112189875",                                 # fails the Luhn check, no separator
    "https://example.org och https://example.org/",        # nothing past the host
])
def test_masking_leaves_dates_amounts_order_numbers_and_bare_hosts_alone(text):
    assert mask.mask(text) == text


# ---------------------------------------------------------------- records

T0 = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)


def _m(i, text, *, who="Anna Berg", out=False, medium="email", subject="Offert", minutes=0):
    return {"id": i, "account_id": "work" if medium == "email" else "teams", "medium": medium, "received_at": T0 + timedelta(minutes=minutes),
            "direction": "out" if out else "in", "from_name": who, "from_address": f"{who.split()[0].lower()}@kund.se",
            "subject": subject, "text": text if medium != "email" else None, "quote_stripped": text,
            "body_text": text, "chat_type": "group" if medium != "email" else None, "attachments": []}


def _item(unit, anchor, context=(), before=(), **extra):
    return {"position": 1, "unit": unit, "message": anchor, "context": list(context), "before": list(before), **extra}


def test_the_message_record_is_the_anchor_alone_masked_with_its_text_capped():
    a = _m(1, "Hej! Mitt personnummer är 19811218-9876, ring 070-123 45 67. " + "Lång text. " * 400)
    a["participants"] = [{"role": "to", "address": "o@company.example", "name": "Alex Lind"},
                         {"role": "cc", "address": "boss@kund.se", "name": "Chef"}]
    a["attachments"] = [{"filename": "Offert 2026.pdf"}]
    rec = jev.record(_item("thread", a, [_m(2, "tidigare")]), "message")
    mine = personal.owner()["sent_by_key"]
    assert mine == "sent_by_you"  # owner.json can rename it
    assert set(rec) == {"unit", "medium", "account", "date", mine, "from", "to", "cc", "subject", "attachments", "text"}
    assert rec["unit"] == "message" and rec["account"] == "work mail" and rec["medium"] == "email"
    assert rec["from"] == "Anna Berg <anna@kund.se>" and rec["to"] == ["Alex Lind <o@company.example>"]
    assert rec["date"] == "2026-09-20 09:00" and rec[mine] is False
    assert len(rec["text"]) == jev.BODY_MAX and rec["text"].endswith("…")
    assert "[personnummer]" in rec["text"] and "[phone]" in rec["text"]
    body = jev.encode(jev.request_body(rec, QUESTIONS)).decode()
    assert "9876" not in body and "123 45 67" not in body and "tidigare" not in body


def test_the_context_record_adds_the_thread_nearest_first_and_stays_within_its_cap():
    thread = [_m(i, f"Meddelande {i}. " + "Detaljer om offerten. " * 30, minutes=i) for i in range(1, 12)]
    anchor = dict(thread[6])
    anchor["quote_stripped"] = anchor["body_text"] = "Svar på offerten. " * 30
    anchor["participants"] = []
    thread[6] = anchor
    it = _item("thread", anchor, thread, thread_messages=11)
    rec = jev.record(it, "context")
    assert jev.record_chars(rec) <= jev.RECORD_MAX
    ctx = rec["context"]
    assert ctx["kind"].startswith("the rest of the thread (11 messages")
    assert 0 < len(ctx["lines"]) < 10 and ctx["not_shown"] == 10 - len(ctx["lines"])
    shown = [int(line.split("Meddelande ")[1].split(".")[0]) for line in ctx["lines"]]
    assert shown == sorted(shown) and 6 in shown and 8 in shown   # the neighbours of the anchor
    assert all(len(line) <= jev.LINE_MAX for line in ctx["lines"])
    assert "Svar på offerten" in rec["text"]  # the anchor itself is the text, never a context line
    assert not any("Svar på offerten" in line for line in ctx["lines"])
    # the message unit of the same item has no context
    assert "context" not in jev.record(it, "message")
    # a very long anchor gives up some of its text so the context keeps room
    anchor["quote_stripped"] = anchor["body_text"] = "Svar på offerten. " * 200
    rec = jev.record(it, "context")
    assert jev.record_chars(rec) <= jev.RECORD_MAX and rec["context"]["lines"]
    anchor["participants"] = [{"role": "cc", "address": f"person{i}@kund.se", "name": "Namn " * 20} for i in range(6)]
    rec = jev.record(it, "context")
    assert jev.record_chars(rec) <= jev.RECORD_MAX and len(rec["text"]) < jev.BODY_MAX and rec["context"]["lines"]


def _chat(n=5, text="rad {i}"):
    chat = [_m(i, text.format(i=i), medium="teams_chat", subject="Driftchatt", minutes=i, out=(i == 3))
            for i in range(1, n + 1)]
    before = [_m(100 + i, f"innan {i}", medium="teams_chat", subject="Driftchatt", minutes=-300 + i) for i in range(3)]
    return chat, before


def test_a_teams_item_is_its_whole_window_with_speakers_times_and_the_lines_before_it():
    chat, before = _chat()
    rec = jev.record(_item("window", chat[1], chat, before), "context")
    assert rec["unit"] == "window" and "text" not in rec and "context" not in rec
    assert rec["medium"] == "Teams group chat" and rec["account"] == "work Teams" and rec["chat"] == "Driftchatt"
    w = rec["window"]
    assert "gap of more than 2 hours" in w["kind"] and "not_shown" not in w
    assert [x.split(": ", 1)[1] for x in w["lines"]] == ["rad 1", "rad 2", "rad 3", "rad 4", "rad 5"]  # in order
    assert w["lines"][1] == "→ 2026-09-20 09:02 Anna Berg: rad 2"   # the anchor, marked, with time and speaker
    assert w["lines"][2] == "2026-09-20 09:03 You: rad 3"           # the owner's own line
    assert [x.split("innan ")[1] for x in w["before_window"]] == ["0", "1", "2"]
    # an account without a jev_label: a Teams message's is "Teams", a mail's is the account's id
    bare = [dict(m, account_id="other") for m in chat]
    assert jev.record(_item("window", bare[1], bare, before), "context")["account"] == "Teams"
    assert jev.record(_item("message", _m(1, "x") | {"account_id": "other"}), "message")["account"] == "other"


def test_a_teams_items_record_is_the_same_window_in_both_unit_designs():
    chat, before = _chat()
    it = _item("window", chat[3], chat, before)
    assert jev.record(it, "message") == jev.record(it, "context")
    # a long window is capped at 30 lines and 2,500 characters, the lines nearest the anchor kept
    long_chat, before = _chat(n=60, text="rad {i} " + "ord " * 20)
    it = _item("window", long_chat[40], long_chat, before)
    rec = jev.record(it, "message")
    assert rec == jev.record(it, "context") and jev.record_chars(rec) <= jev.RECORD_MAX
    w = rec["window"]
    assert len(w["lines"]) <= enrich.TEAMS_WINDOW_CAP and w["not_shown"] == 60 - len(w["lines"])
    kept = [int(x.split("rad ")[1].split()[0]) for x in w["lines"]]
    assert kept == sorted(kept) and 41 in kept and max(abs(k - 41) for k in kept) <= len(kept) // 2 + 1


def test_a_pattern_item_shows_the_patterns_size_and_its_other_samples():
    samples = [_m(i, f"Din faktura {i}", subject=f"Faktura {i}", minutes=i) for i in (2, 3)]
    it = _item("pattern", _m(1, "Din faktura 1", subject="Faktura 1"), samples,
               pattern={"message_count": 40, "thread_count": 40, "first_at": T0, "last_at": T0})
    ctx = jev.record(it, "context")["context"]
    assert "(40 messages in 40 threads" in ctx["kind"] and len(ctx["lines"]) == 2 and "[Faktura 2]" in ctx["lines"][0]
    single = jev.record(_item("message", _m(1, "x")), "context")
    assert single["context"] == {"kind": "none: a single message"}


def test_records_from_a_real_answer_key_fit_their_caps_in_both_units(conn, ingestor, taxonomy_loaded):
    _full(conn, ingestor)
    sid = gold.sample(conn, n=12, seed=4)["set_id"]
    units = set()
    for i in _items(conn, sid):
        it = gold.item(conn, sid, i["position"])
        units.add(it["unit"])
        m, c = jev.record(it, "message"), jev.record(it, "context")
        assert jev.record_chars(c) <= jev.RECORD_MAX
        if it["unit"] == "window":  # Teams: the window, the same in both designs
            assert m == c and m["unit"] == "window" and m["window"]["lines"]
            assert any(x.startswith("→ ") for x in m["window"]["lines"])
            continue
        assert "context" not in m and len(m["text"]) <= jev.BODY_MAX and c["context"]["kind"]
        assert {k: v for k, v in c.items() if k not in ("context", "unit", "text")} == \
               {k: v for k, v in m.items() if k not in ("unit", "text")}
    assert {"window", "pattern"} <= units


# ---------------------------------------------------------------- the client

def _decide(c: jev.JevClient, body=None) -> dict:
    async def go():
        async with httpx.AsyncClient(transport=c._transport) as http:
            return await c.decide(http, body or jev.request_body({"text": "Hej"}, QUESTIONS))
    return asyncio.run(go())


def test_building_the_client_reads_no_secret_and_the_first_request_reads_the_keychain_once(keychain):
    fake = FakeJev()
    c = client(fake)
    assert keychain == []  # built, nothing read
    _decide(c)
    _decide(c)
    assert keychain == [jev.KEY_NAME]  # read once, from the Keychain item typesafe-api-key
    assert all(h["authorization"] == f"Bearer {FAKE_KEY}" for h in fake.headers)  # never the environment's
    assert FAKE_KEY not in repr(c)
    assert c.usage.as_dict() == {"requests": 2, "retries": 0, "input_tokens": 10000, "output_tokens": 800}


def test_a_missing_key_says_how_to_add_it_and_nothing_is_sent(monkeypatch):
    monkeypatch.setattr(secrets.keyring, "get_password", lambda service, key: None)
    fake = FakeJev()
    with pytest.raises(secrets.MissingSecret, match="security add-generic-password -s talos -a typesafe-api-key"):
        _decide(client(fake))
    assert fake.requests == []


def test_a_429_is_retried_after_the_wait_retry_after_asks_for_capped(keychain):
    waits = []

    async def sleep(s):
        waits.append(s)
    fake = FakeJev(statuses=[429, 429], retry_after="7")
    c = client(fake, sleep=sleep)
    assert _decide(c)["answers"]
    assert waits == [7.0, 7.0] and len(fake.requests) == 3 and c.usage.retries == 2
    fake = FakeJev(statuses=[429], retry_after="3600")  # an hour is capped
    waits.clear()
    _decide(client(fake, sleep=sleep, max_delay=30))
    assert waits == [30]
    stamp = time.time() + 12
    assert 10 <= jev.retry_after(time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(stamp))) <= 12.5


def test_a_5xx_is_retried_with_backoff_and_given_up_after_max_retries(keychain):
    waits = []

    async def sleep(s):
        waits.append(s)
    fake = FakeJev(statuses=[503, 500, 502])
    assert _decide(client(fake, sleep=sleep))["answers"]
    assert waits == [0.5, 1.0, 2.0]
    fake = FakeJev(statuses=[500] * 10)
    with pytest.raises(jev.JevError, match="HTTP 500 after 2 retries"):
        _decide(client(fake, sleep=sleep, max_retries=2))
    assert len(fake.requests) == 3
    fake = FakeJev(statuses=[400])  # a client error is not retried
    with pytest.raises(jev.JevError, match="HTTP 400"):
        _decide(client(fake, sleep=sleep))
    assert len(fake.requests) == 1


def test_a_network_error_is_retried(keychain):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectTimeout("slow")
        return FakeJev().handle(request)
    c = jev.JevClient(transport=httpx.MockTransport(handler), sleep=_no_sleep)
    assert _decide(c)["answers"] and len(calls) == 3


def test_a_refused_key_stops_the_run_and_never_shows_the_key(keychain):
    fake = FakeJev(statuses=[401] * 50)
    c = client(fake, concurrency=1)
    results = []
    cases = [(i, jev.request_body({"text": str(i)}, QUESTIONS)) for i in range(5)]
    with pytest.raises(jev.JevAuthError) as exc:
        asyncio.run(c.run(cases, results.append))
    assert "typesafe-api-key" in str(exc.value) and FAKE_KEY not in str(exc.value)
    assert len(fake.requests) == 1 and results == []  # the other four were never sent


def test_the_client_keeps_eight_requests_in_flight(keychain):
    active, peak = [0], [0]

    async def handler(request):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0.01)
        active[0] -= 1
        return FakeJev().handle(request)
    c = jev.JevClient(transport=httpx.MockTransport(handler))
    results = []
    asyncio.run(c.run([(i, jev.request_body({"text": str(i)}, QUESTIONS)) for i in range(30)], results.append))
    assert len(results) == 30 and peak[0] == jev.CONCURRENCY == 8
    assert all(r.error is None and r.usage["input_tokens"] == 5000 for r in results)


# ---------------------------------------------------------------- the routing policy

def test_a_one_value_field_is_decided_only_over_the_probability_and_the_margin():
    d = policy.route_one("origin", {"person": 0.80, "marketing": 0.15, "list": 0.05})
    assert (d.decided, d.top, d.selected, d.confidence, d.margin) == (True, "person", ("person",), 0.80, 0.65)
    low = policy.route_one("origin", {"person": 0.65, "marketing": 0.35})
    assert (low.decided, low.top, low.selected) == (False, "person", ())   # the candidate is kept
    close = policy.route_one("origin", {"person": 0.72, "marketing": 0.60})
    assert not close.decided and round(close.margin, 2) == 0.12
    edge = policy.route_one("origin", {"person": 0.70, "marketing": 0.55})
    assert edge.decided  # exactly on both thresholds
    assert policy.route_one("origin", {"person": 0.65, "marketing": 0.35}, Policy(one_min=0.6)).decided


def test_a_many_value_field_selects_every_statement_at_or_over_the_threshold():
    d = policy.route_many("ask", {"question": 0.9, "action": 0.5, "decision": 0.3, "deadline": 0.05})
    assert d.decided and d.selected == ("question", "action") and d.top == "question"
    assert d.confidence == 0.5  # the closest call
    assert policy.route_many("ask", {"question": 0.2, "action": 0.1}).selected == ()   # none
    assert policy.route_many("ask", {"question": 0.9, "action": 0.5}, Policy(many_min=0.6)).selected == ("question",)
    assert policy.DEFAULT == Policy(0.70, 0.15, 0.5) and policy.DEFAULT.version == policy.VERSION


# ---------------------------------------------------------------- runs

def _answer_key(conn, ingestor, n=8):
    _full(conn, ingestor)
    sid = gold.sample(conn, n=n, seed=4)["set_id"]
    conn.commit()  # the command line reads it on a connection of its own
    return sid


PICK = {"origin": ("marketing", 0.9), "type": ("promotion", 0.6), "topic": ("Shopping", 0.95),
        "value": ("noise", 0.8), "ask:question": 0.2, "route:Costs": 0.7}


def test_a_run_stores_every_case_with_its_answers_routing_and_usage_and_writes_no_assignment(
        conn, ingestor, taxonomy_loaded, keychain):
    sid = _answer_key(conn, ingestor)
    assignments = conn.execute("select count(*) as n from assignment").fetchone()["n"]
    fake = FakeJev(pick=PICK)
    said = []
    res = jev_gold.run(conn, sid, "context", client=client(fake), progress=said.append, template=1)
    assert res["stored"] == 8 and res["failed"] == [] and len(fake.requests) == 8
    assert said[0].startswith(f"jev: run {res['run_id']}: 8 cases to send") and said[-1].startswith("jev: 8/8")
    assert all(("context" in r["state"]) if r["state"]["unit"] == "context" else r["state"]["unit"] == "window"
               for r in fake.requests)
    run = conn.execute("select * from model_run where id = %s", (res["run_id"],)).fetchone()
    p = run["params"]
    assert (run["model"], run["purpose"], run["input_count"], run["output_count"]) == (jev.MODEL, "gold-eval", 8, 8)
    assert (p["set_id"], p["unit"], p["question_version"], p["template_version"]) == (
        sid, "context", jev.question_version(QUESTIONS_V1, template=1), 1)
    assert all(r["questions"] == QUESTIONS_V1 for r in fake.requests)
    assert (p["input_tokens"], p["output_tokens"], p["requests"], p["cases_done"]) == (40000, 3200, 8, 8)
    assert p["cost_usd"] == pytest.approx(40000 * 0.042 / 1e6) and p["wall_seconds"] > 0
    assert p["policy"] == Policy().as_dict()
    preds = {(r["field"]): r for r in conn.execute("select * from jev_prediction where run_id = %s and item_id ="
                                                   " (select min(item_id) from jev_case where run_id = %s)",
                                                   (res["run_id"], res["run_id"]))}
    assert set(preds) == set(gold.FIELDS)
    assert (preds["origin"]["top"], preds["origin"]["selected"], preds["origin"]["decided"]) == (
        "marketing", ["marketing"], True)
    assert (preds["type"]["top"], preds["type"]["selected"], preds["type"]["decided"]) == ("promotion", [], False)
    assert preds["type"]["confidence"] == 0.6 and preds["type"]["scores"]["promotion"] == 0.6
    assert preds["route"]["selected"] == ["Costs"] and preds["route"]["decided"]
    assert preds["ask"]["selected"] == [] and preds["ask"]["raw"]["question"] == {"type": "noul", "noul": 0.2}
    case = conn.execute("select * from jev_case where run_id = %s limit 1", (res["run_id"],)).fetchone()
    assert (case["input_tokens"], case["output_tokens"], case["model"]) == (5000, 400, jev.MODEL)
    assert len(case["record_sha256"]) == 64 and case["record_chars"] <= jev.RECORD_MAX
    assert conn.execute("select count(*) as n from assignment").fetchone()["n"] == assignments


def test_a_rerun_of_the_same_run_skips_the_cases_already_done(conn, ingestor, taxonomy_loaded, keychain):
    sid = _answer_key(conn, ingestor)
    marked = []

    def pick(body):  # the fake fails every other case the first time
        return PICK
    fake = FakeJev(pick=pick)
    base = fake.handle

    def flaky(request):
        n = len(fake.requests)
        if n % 2:
            fake.requests.append(json.loads(request.content))
            marked.append(json.loads(request.content)["state"]["text"])
            return httpx.Response(500, text="down")
        return base(request)
    fake.transport = httpx.MockTransport(flaky)
    first = jev_gold.run(conn, sid, "message", client=client(fake, max_retries=0, concurrency=1),
                         progress=lambda s: None)
    assert first["stored"] == 4 and len(first["failed"]) == 4
    assert all("HTTP 500" in f["error"] for f in first["failed"])
    fake2 = FakeJev(pick=PICK)
    again = jev_gold.run(conn, sid, "message", client=client(fake2), run_id=first["run_id"], progress=lambda s: None)
    assert again["already_done"] == 4 and again["to_send"] == 4 and again["stored"] == 4
    assert sorted(r["state"]["text"] for r in fake2.requests) == sorted(marked)  # only the four that failed
    p = conn.execute("select params, output_count from model_run where id = %s", (first["run_id"],)).fetchone()
    assert p["output_count"] == 8 and p["params"]["cases_done"] == 8 and p["params"]["invocations"] == 2
    assert p["params"]["requests"] == 12 and p["params"]["input_tokens"] == 8 * 5000
    fake3 = FakeJev(pick=PICK)
    done = jev_gold.run(conn, sid, "message", client=client(fake3), run_id=first["run_id"], progress=lambda s: None)
    assert done["to_send"] == 0 and fake3.requests == []
    # the same run with other records or questions is refused
    with pytest.raises(jev_gold.JevRunError, match="another unit"):
        jev_gold.run(conn, sid, "context", client=client(fake3), run_id=first["run_id"])
    with pytest.raises(jev_gold.JevRunError, match="another question_version, template_version"):
        jev_gold.run(conn, sid, "message", client=client(fake3), run_id=first["run_id"], template=1)


def test_the_cost_guard_refuses_a_run_over_max_cases_before_anything_is_sent(conn, ingestor, taxonomy_loaded,
                                                                            keychain):
    sid = _answer_key(conn, ingestor)
    fake = FakeJev()
    with pytest.raises(jev_gold.JevRunError, match="8 cases to send is over --max-cases 5"):
        jev_gold.run(conn, sid, "message", client=client(fake), max_cases=5)
    assert fake.requests == [] and keychain == []
    assert conn.execute("select count(*) as n from model_run").fetchone()["n"] == 0
    res = jev_gold.run(conn, sid, "message", client=client(fake), max_cases=5, limit=5, progress=lambda s: None)
    assert res["stored"] == 5 and len(fake.requests) == 5


def test_the_dry_run_shows_one_whole_request_and_the_estimate_and_sends_nothing(
        conn, ingestor, taxonomy_loaded, database, vault, monkeypatch, capsys):
    sid = _answer_key(conn, ingestor)
    monkeypatch.setattr(secrets, "get", lambda key: pytest.fail("a dry run read a secret"))
    monkeypatch.setattr(jev.JevClient, "run", lambda *a, **k: pytest.fail("a dry run sent something"))
    monkeypatch.setattr(httpx.AsyncClient, "post", lambda *a, **k: pytest.fail("a dry run sent something"))
    res = jev_gold.run(conn, sid, "context", dry_run=True)
    assert res["dry_run"] and res["to_send"] == 8 and res["request"]["questions"] == QUESTIONS
    assert res["request"]["state"]["recipient"] == jev.RECIPIENT and res["request"]["state"]["unit"] == "context"
    assert res["estimated_input_tokens"] > 8 * 5000 and res["estimated_cost_usd"] > 0
    assert conn.execute("select count(*) as n from model_run").fetchone()["n"] == 0
    monkeypatch.setenv("TALOS_DSN", database)
    monkeypatch.setenv("TALOS_HOME", str(vault.root.parent))
    monkeypatch.setattr(cli, "_logging", lambda settings, verbose: None)
    cli.main(["enrich", "jev", "gold", "--set", str(sid), "--unit", "message", "--dry-run", "--max-cases", "3"])
    out = capsys.readouterr().out
    assert out.startswith("DRY RUN: nothing is sent, no key is read, nothing is stored.")
    assert "8 cases would be sent" in out and "REFUSED: over --max-cases" in out
    assert f"POST {jev.ENDPOINT}" in out and "not read in a dry run" in out
    shown = json.loads(out[out.index("\n{") + 1:])
    assert shown["model"] == jev.MODEL and shown["state"]["unit"] == "message" and shown["questions"] == QUESTIONS
    assert conn.execute("select count(*) as n from model_run").fetchone()["n"] == 0


def test_a_run_from_the_command_line_prints_progress_and_the_report_command(
        conn, ingestor, taxonomy_loaded, database, vault, monkeypatch, capsys, keychain):
    sid = _answer_key(conn, ingestor, n=6)
    fake = FakeJev(pick=PICK)
    real = jev.JevClient
    monkeypatch.setattr(jev, "JevClient", lambda **kw: real(transport=fake.transport, sleep=_no_sleep, **kw))
    monkeypatch.setenv("TALOS_DSN", database)
    monkeypatch.setenv("TALOS_HOME", str(vault.root.parent))
    monkeypatch.setattr(cli, "_logging", lambda settings, verbose: None)
    cli.main(["enrich", "jev", "gold", "--set", str(sid), "--unit", "message"])
    out = capsys.readouterr().out
    assert "jev: 6/6 (6 stored, 0 failed)" in out and "report: talos enrich jev report --run jev-gold" in out
    run_id = out.split("report: talos enrich jev report --run ")[1].split()[0]
    cli.main(["enrich", "jev", "report", "--run", run_id])
    out = capsys.readouterr().out
    assert f"Jev on answer key {sid}: 6 items scored" in out and "jev-report-" in out
    with pytest.raises(SystemExit):
        cli.main(["enrich", "jev", "report"])
    assert "usage: talos enrich jev report --run RUN" in capsys.readouterr().err


# ---------------------------------------------------------------- the report

def _fake_run(conn, sid, run_id, unit, rows, tokens=1000, template=None):
    """A stored run: rows = {position: {field: scores}}; routed with the default policy, as the
    templates ask (v1 when template is None, and then no template_version is stored, as a run
    from before it was recorded)."""
    ids = {r["position"]: r["id"] for r in conn.execute("select id, position from gold_item where set_id = %s", (sid,))}
    params = {"set_id": sid, "unit": unit, "question_version": "q", "policy": Policy().as_dict(),
              "cases_done": len(rows), "requests": len(rows), "retries": 0, "failed": 0,
              "input_tokens": tokens * len(rows), "output_tokens": 0,
              "cost_usd": tokens * len(rows) * 0.042 / 1e6, "wall_seconds": 2.0}
    if template is not None:
        params["template_version"] = template
    conn.execute("insert into model_run (id, model, purpose, params, input_count, output_count)"
                 " values (%s, 'jev-1.13.0', 'gold-eval', %s, %s, %s)",
                 (run_id, json.dumps(params), len(rows), len(rows)))
    for pos, fields in rows.items():
        conn.execute("insert into jev_case (run_id, item_id, record_sha256, record_chars, model, input_tokens)"
                     " values (%s, %s, 'x', 100, 'jev-1.13.0', %s)", (run_id, ids[pos], tokens))
        routed, _ = policy.route_case(fields, template=template or 1)
        for f, scores in fields.items():
            d = routed[f]
            conn.execute("insert into jev_prediction (run_id, item_id, field, top, selected, scores, confidence,"
                         " margin, decided, raw) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, '{}')",
                         (run_id, ids[pos], f, d.top, list(d.selected), json.dumps(scores), d.confidence, d.margin,
                          d.decided))


def test_the_report_scores_decided_cases_against_the_owners_label_else_claudes_and_leaves_not_sure_out(
        conn, ingestor, taxonomy_loaded):
    sid = _answer_key(conn, ingestor, n=6)
    # Claude labels all six; the owner labels item 1 themselves (marketing, where Claude said person)
    for pos, o in enumerate(["person", "marketing", "marketing", "notification", "alert", "person"], 1):
        gold.save_label(conn, sid, pos, "origin", [o], "unsure" if pos == 6 else "set", labeller="claude")
        gold.save_label(conn, sid, pos, "ask", ["question"] if pos <= 2 else [], labeller="claude")
    gold.save_label(conn, sid, 1, "origin", ["marketing"])
    conn.commit()
    one = lambda v, p, r=0.0: {v: p, "person" if v != "person" else "list": r}  # noqa: E731
    rows = {1: {"origin": one("marketing", 0.9), "ask": {"question": 0.8, "action": 0.1}},       # right (the owner's)
            2: {"origin": one("marketing", 0.75, 0.2), "ask": {"question": 0.3, "action": 0.1}},  # right; ask missed
            3: {"origin": one("person", 0.6, 0.3), "ask": {"question": 0.1, "action": 0.1}},      # undecided, wrong top
            4: {"origin": one("alert", 0.97), "ask": {"question": 0.1, "action": 0.6}},           # notification→alert
            5: {"origin": one("alert", 0.88), "ask": {"question": 0.1, "action": 0.1}},           # right
            6: {"origin": one("person", 0.99), "ask": {"question": 0.1, "action": 0.1}}}          # Claude not sure
    _fake_run(conn, sid, "run-a", "message", rows)
    rep = jev_gold.report(conn, "run-a")
    assert rep["items"] == 6 and rep["reference"]["origin"]["from"] == {"claude": 5, personal.OWNER_ID: 1}
    assert rep["reference"]["origin"]["unsure"] == 1
    o = rep["runs"][0]["scores"]["origin"]
    assert (o["labelled"], o["unsure"]) == (5, 1)                   # not sure: left out, counted
    assert (o["decided"], o["correct"], o["coverage"], o["accuracy"]) == (4, 3, 0.8, 0.75)
    assert (o["top1_n"], o["top1_correct"]) == (5, 3)
    assert o["mistakes"] == {"alert→notification": 1}
    assert o["mistakes_all"] == {"alert→notification": 1, "person→marketing": 1}
    assert o["by_bucket"]["0.95–1.00"] == {"n": 1, "correct": 0, "precision": 0.0}
    a = rep["runs"][0]["scores"]["ask"]
    assert a["any"]["tp"] == 1 and a["any"]["fn"] == 1 and a["any"]["fp"] == 1 and a["exact"] == pytest.approx(0.6667, 1e-3)
    # the sweep re-routes the stored probabilities: at 0.5 with no margin all five are decided
    sw = {(p["min"], p["margin"]): p for p in rep["runs"][0]["sweep"]["origin"]}
    assert (sw[0.5, 0.0]["coverage"], sw[0.5, 0.0]["accuracy"]) == (1.0, 0.6)
    assert (sw[0.7, 0.15]["coverage"], sw[0.7, 0.15]["accuracy"]) == (0.8, 0.75)   # the run's own policy
    assert (sw[0.95, 0.4]["coverage"], sw[0.95, 0.4]["accuracy"]) == (0.2, 0.0)
    ask = {p["min"]: p for p in rep["runs"][0]["sweep"]["ask"]}
    assert ask[0.3]["any_recall"] == 1.0 and ask[0.9]["any_recall"] == 0.0
    # email and Teams are scored apart as well, on their own items
    media = {r["position"]: r["medium"] for r in conn.execute(
        "select i.position, m.medium from gold_item i join message m on m.id = i.message_id where i.set_id = %s", (sid,))}
    teams = {p for p, m in media.items() if m != "email"}
    assert teams and len(teams) < 6  # both media are in the sample
    assert rep["split_items"] == {"all": 6, "email": 6 - len(teams), "teams": len(teams)}
    sp = rep["runs"][0]["splits"]
    assert sp["email"]["items"] + sp["teams"]["items"] == 6
    assert sp["email"]["scores"]["origin"]["labelled"] + sp["teams"]["scores"]["origin"]["labelled"] == 5
    assert sp["teams"]["scores"]["origin"]["labelled"] == len(teams - {6})
    c = rep["runs"][0]["cost"]
    assert (c["cases"], c["input_tokens"], c["input_tokens_per_case"], c["wall_seconds"]) == (6, 6000, 1000, 2.0)
    text = jev_gold.format_report(rep)
    assert "accuracy on decided 3/4 (75.0%)" in text and "alert→notification 1" in text
    assert "Threshold sweep" in text and "$0.0003" in text
    assert "    0.70  0.15  80.0% / 75.0%" in text   # the run's own policy, as a row of the sweep


def test_two_runs_are_compared_side_by_side_on_the_items_both_answered(conn, ingestor, taxonomy_loaded):
    sid = _answer_key(conn, ingestor, n=6)
    for pos in range(1, 7):
        gold.save_label(conn, sid, pos, "origin", ["marketing"], labeller="claude")
    conn.commit()
    _fake_run(conn, sid, "run-m", "message", {p: {"origin": {"marketing": 0.6, "person": 0.4}} for p in range(1, 7)})
    _fake_run(conn, sid, "run-c", "context", {p: {"origin": {"marketing": 0.9, "person": 0.1}} for p in range(1, 5)},
              tokens=1500)
    rep = jev_gold.report(conn, "run-m", compare_run="run-c")
    assert rep["items"] == 4 and [r["unit"] for r in rep["runs"]] == ["message", "context"]
    m, c = (r["scores"]["origin"] for r in rep["runs"])
    assert (m["coverage"], m["top1"]) == (0.0, 1.0) and (c["coverage"], c["accuracy"]) == (1.0, 1.0)
    text = jev_gold.format_report(rep)
    assert "Side by side: message (run-m)  vs  context (run-c)" in text
    assert "origin  coverage" in text and "input tokens per case" in text
    assert "a Teams item is the same window in both designs" in text
    assert text.index("  email (") < text.index("  teams (") < text.index("  all (4 items)")
    with pytest.raises(jev_gold.JevRunError, match="no Jev answer-key run"):
        jev_gold.report(conn, "run-m", compare_run="nope")


def test_the_combined_reference_prefers_the_owners_answer_field_by_field(conn, ingestor, taxonomy_loaded):
    sid = _answer_key(conn, ingestor, n=6)
    gold.save_label(conn, sid, 1, "origin", ["person"], labeller="claude")
    gold.save_label(conn, sid, 1, "type", ["request"], labeller="claude")
    gold.save_label(conn, sid, 1, "origin", ["list"], "unsure")  # the owner's, even when they are not sure
    ref, src = gold.combined_labels(conn, sid)
    iid = _items(conn, sid)[0]["id"]
    assert ref[iid]["origin"] == {"values": ["list"], "status": "unsure"} and ref[iid]["type"]["values"] == ["request"]
    assert src["origin"] == {personal.OWNER_ID: 1} and src["type"] == {"claude": 1}


# ---------------------------------------------------------------- question templates v2

def test_v2_asks_ask_as_one_choice_with_none_and_a_deadline_statement_beside_it():
    q = QUESTIONS["ask"]
    assert q["type"] == "choice"
    assert list(q["criteria"]) == ["none", "question", "action", "decision", "my_commitment"]
    assert q["criteria"]["none"] == "Nothing is asked of you personally and you promise nothing."
    assert q["criteria"]["action"] == "Action for you: Someone asks you to do, send or fix something."
    for phrase in ("aimed at you personally", "a person writing to you",
                   "a notice about your own account or service that requires you to act",
                   "Mass mailings, newsletters, marketing, cold sales, and notices to many never ask",
                   "pick the main ask"):
        assert phrase in q["instructions"]
    assert QUESTIONS["ask:deadline"] == {"type": "noul",
                                         "instructions": "It carries a date by which you must act."}
    assert not any(k.startswith("ask:") and k != "ask:deadline" for k in QUESTIONS)


def test_v2_asks_route_as_one_choice_of_none_and_the_fifteen_work_categories():
    q = QUESTIONS["route"]
    assert q["type"] == "choice"
    assert list(q["criteria"]) == ["none"] + [v["value"] for v in FILE["route"]["values"]]
    assert len(q["criteria"]) == 16 and q["criteria"]["none"] == "Not work mail, or no clear work category."
    assert "Pick the ONE main work category" in q["instructions"]
    assert "IT Operations only when it is really about running IT day to day" in q["instructions"]
    assert not any(k.startswith("route:") for k in QUESTIONS)
    assert list(QUESTIONS) == ["origin", "type", "topic", "ask", "ask:deadline", "value", "route"]


def test_v2_asks_a_teams_window_its_own_set_without_origin_and_with_a_short_type_list():
    email, teams = QSETS["email"], QSETS["teams"]
    assert email == QUESTIONS
    assert list(teams) == ["type", "topic", "ask", "ask:deadline", "value", "route"]   # origin is not asked
    assert tuple(teams["type"]["criteria"]) == jev.TEAMS_VALUES["type"] == (
        "conversation", "question", "request", "fyi", "announcement", "reply_thanks", "meeting_notes", "document",
        "invitation", "incident", "other")
    assert "value" not in jev.TEAMS_VALUES                                          # value keeps its full list
    assert tuple(teams["value"]["criteria"]) == tuple(email["value"]["criteria"])
    assert teams["type"]["criteria"]["fyi"] == email["type"]["criteria"]["fyi"]      # the same definitions
    assert teams["topic"]["criteria"] == email["topic"]["criteria"]                 # topic, ask, route: as for email
    assert teams["ask"]["criteria"] == email["ask"]["criteria"]
    assert teams["route"]["criteria"] == email["route"]["criteria"]
    assert all(q["instructions"].startswith(jev.TEAMS_PREAMBLE) for q in teams.values() if q["type"] == "choice")
    assert "chat window between colleagues" in jev.TEAMS_PREAMBLE and "as a whole conversation" in jev.TEAMS_PREAMBLE
    assert "marked →" in jev.TEAMS_PREAMBLE
    assert not any(q["instructions"].startswith(jev.TEAMS_PREAMBLE) for q in email.values())
    assert teams["ask:deadline"] == email["ask:deadline"]
    # a Teams list naming a value the taxonomy lacks is refused, not silently shortened
    dims = json.loads(json.dumps(DIMS))
    dims["type"]["values"] = [v for v in dims["type"]["values"] if v["value"] != "fyi"]
    with pytest.raises(jev.JevError, match="the Teams list of type has values the taxonomy lacks: fyi"):
        jev.build_questions(dims, teams=True)


def test_the_question_version_changes_with_the_templates_and_covers_both_sets():
    assert jev.TEMPLATE_VERSION == 2
    v2 = jev.question_version(QSETS)
    v1 = jev.question_version(QUESTIONS_V1, template=1)
    assert v2 != v1 and v2 == jev.question_version(jev.question_sets(DIMS))
    assert jev.question_version(QUESTIONS_V1, template=2) != v1          # the template version is hashed too
    changed = dict(QSETS, teams=dict(QSETS["teams"], value={**QSETS["teams"]["value"], "instructions": "x"}))
    assert jev.question_version(changed) != v2                           # a Teams question alone changes it


def test_v2_answers_carry_the_choice_with_none_and_the_deadline_and_a_teams_origin_fixed_as_person():
    got = jev.read_answers(DIMS, answer(QUESTIONS, {"ask": ("action", 0.8), "ask:deadline": 0.6,
                                                    "route": ("none", 0.9)}))
    assert got["ask"]["many"] and got["ask"]["scores"]["action"] == 0.8 and got["ask"]["scores"]["deadline"] == 0.6
    assert set(got["ask"]["scores"]) == {"none", "question", "action", "decision", "my_commitment", "deadline"}
    assert set(got["ask"]["raw"]) == {"choice", "deadline"} and got["ask"]["raw"]["choice"]["choice"] == "action"
    assert got["route"]["scores"]["none"] == 0.9 and len(got["route"]["scores"]) == 16
    teams = jev.read_answers(DIMS, answer(QSETS["teams"], {"type": ("fyi", 0.9)}), teams=True)
    assert teams["origin"] == {"many": False, "scores": {"person": 1.0},
                               "raw": {"fixed": "person", "why": "a Teams window: origin is not asked"}}
    assert set(teams["type"]["scores"]) == set(jev.TEAMS_VALUES["type"])
    d = policy.route_one("origin", teams["origin"]["scores"])
    assert (d.top, d.selected, d.decided) == ("person", ("person",), True)
    bad = answer(QSETS["teams"])
    bad["type"]["probabilities"]["promotion"] = 0.0      # not on the Teams list
    with pytest.raises(jev.JevError, match="type has an unknown value 'promotion'"):
        jev.read_answers(DIMS, bad, teams=True)
    bad = answer(QUESTIONS)
    del bad["ask:deadline"]
    with pytest.raises(jev.JevError, match="no score for ask:deadline"):
        jev.read_answers(DIMS, bad)
    bad = answer(QUESTIONS)
    bad["route"]["probabilities"]["Invented"] = 0.0
    with pytest.raises(jev.JevError, match="route has an unknown value 'Invented'"):
        jev.read_answers(DIMS, bad)


def _ch(top, p, other="none", **side):
    """A choice's probabilities: top at p, `other` the rest; side statements beside them."""
    return {top: p, other: round(1 - p, 6), **side}


def test_a_many_value_choice_selects_its_top_only_when_decided_and_not_none():
    r = policy.route_choice_many
    d = r("route", _ch("Security", 0.8))
    assert (d.top, d.selected, d.decided, d.confidence, round(d.margin, 2)) == (
        "Security", ("Security",), True, 0.8, 0.6)
    assert r("route", _ch("none", 0.9, "Security")).selected == ()                     # none is no selection
    low = r("route", _ch("Security", 0.6))
    assert (low.top, low.selected, low.decided) == ("Security", (), False)             # undecided: none
    assert r("route", _ch("Security", 0.6), Policy(one_min=0.5)).selected == ("Security",)
    assert r("route", {"none": 0.05, "Security": 0.48, "Costs": 0.47}, Policy(one_min=0.0, one_margin=0.0)
             ).selected == ("Security",)


def test_the_deadline_is_selected_only_beside_an_ask():
    r = lambda scores, pol=policy.DEFAULT: policy.route_choice_many("ask", scores, pol, ("deadline",))  # noqa: E731
    assert r(_ch("action", 0.8, deadline=0.7)).selected == ("action", "deadline")
    assert r(_ch("action", 0.8, deadline=0.4)).selected == ("action",)
    assert r(_ch("none", 0.9, "action", deadline=0.95)).selected == ()             # a deadline without an ask
    assert r(_ch("action", 0.6, deadline=0.95)).selected == ()                     # an undecided ask: none
    assert r(_ch("action", 0.8, deadline=0.55), Policy(many_min=0.6)).selected == ("action",)
    d = r(_ch("action", 0.8, deadline=0.99))
    assert d.top == "action" and d.confidence == 0.8   # the choice's, not the statement's


def _case(origin="person", topic="Work/Security", ask=("question", 0.8), route=("Security", 0.8), deadline=0.1):
    return {"origin": _ch(origin, 0.9, "list" if origin != "list" else "person"),
            "topic": _ch(topic, 0.9, "Family" if topic != "Family" else "Work/AI"),
            "ask": _ch(*ask, deadline=deadline), "route": _ch(*route)}


def test_the_ask_gate_forces_none_unless_jevs_origin_is_a_person_a_person_via_a_system_or_a_list():
    for o in ("person", "person_via_system", "list"):
        d, fired = policy.route_case(_case(origin=o, deadline=0.8), template=2)
        assert d["ask"].selected == ("question", "deadline") and "ask" not in fired
    for o in ("marketing", "notification", "auto_reply", "transactional"):
        d, fired = policy.route_case(_case(origin=o, deadline=0.8), template=2)
        assert d["ask"].selected == () and fired["ask"] == ["ask:origin"]
        assert d["ask"].top == "question"                                  # Jev's answer is kept as the top
    # the top origin counts even when origin itself is undecided
    c = _case(origin="marketing")
    c["origin"] = {"marketing": 0.55, "person": 0.45}
    assert policy.route_case(c, template=2)[1] == {"ask": ["ask:origin"]}
    # a gate only fires when it changes something
    d, fired = policy.route_case(_case(origin="marketing", ask=("none", 0.9)), template=2)
    assert d["ask"].selected == () and "ask" not in fired


def test_the_route_gate_forces_none_off_work_topics_and_for_automatic_replies():
    d, fired = policy.route_case(_case(topic="Shopping"), template=2)
    assert d["route"].selected == () and fired == {"route": ["route:topic"]}
    d, fired = policy.route_case(_case(origin="auto_reply", topic="Work/Meetings"), template=2)
    assert d["route"].selected == () and fired["route"] == ["route:auto_reply"]
    d, fired = policy.route_case(_case(origin="auto_reply", topic="Family"), template=2)
    assert fired["route"] == ["route:auto_reply", "route:topic"]
    d, fired = policy.route_case(_case(topic="Work/Network", route=("Network", 0.9)), template=2)
    assert d["route"].selected == ("Network",) and fired == {}
    assert set(policy.GATES) == {"ask:origin", "route:topic", "route:auto_reply"} and policy.VERSION == 2


def test_v1_scores_are_routed_as_statements_and_never_gated():
    v1 = {"origin": {"marketing": 0.9, "person": 0.1}, "topic": {"Shopping": 0.9, "Family": 0.1},
          "ask": {"question": 0.8, "action": 0.6, "deadline": 0.1}, "route": {"Costs": 0.7, "Security": 0.6}}
    d, fired = policy.route_case(v1, template=1)
    assert d["ask"].selected == ("question", "action") and d["route"].selected == ("Costs", "Security")
    assert fired == {}


def _v2_pick(body):
    """The fake Jev for v2: marketing mail about shopping that asks a question with a deadline;
    a Teams window about the network that asks a question."""
    if "origin" not in body["questions"]:  # the Teams set
        return {"type": ("conversation", 0.9), "topic": ("Work/Network", 0.9), "value": ("context", 0.8),
                "ask": ("question", 0.8), "ask:deadline": 0.2, "route": ("Network", 0.9)}
    return {"origin": ("marketing", 0.9), "type": ("promotion", 0.9), "topic": ("Shopping", 0.95),
            "value": ("noise", 0.8), "ask": ("question", 0.8), "ask:deadline": 0.7, "route": ("Costs", 0.9)}


def test_a_v2_run_asks_teams_windows_their_own_set_fixes_their_origin_and_stores_the_gates(
        conn, ingestor, taxonomy_loaded, keychain):
    sid = _answer_key(conn, ingestor)
    fake = FakeJev(pick=_v2_pick)
    res = jev_gold.run(conn, sid, "context", client=client(fake), progress=lambda s: None)
    assert res["stored"] == 8 and res["template_version"] == 2 and res["questions"] == {"email": 7, "teams": 6}
    teams_reqs = [r for r in fake.requests if r["state"]["unit"] == "window"]
    email_reqs = [r for r in fake.requests if r["state"]["unit"] != "window"]
    assert teams_reqs and email_reqs
    assert all(r["questions"] == QSETS["teams"] for r in teams_reqs)
    assert all(r["questions"] == QSETS["email"] for r in email_reqs)
    p = conn.execute("select params from model_run where id = %s", (res["run_id"],)).fetchone()["params"]
    assert (p["template_version"], p["question_version"]) == (2, jev.question_version(QSETS))
    assert p["policy"]["version"] == policy.VERSION == 2
    preds = {}
    for r in conn.execute("select p.*, m.medium from jev_prediction p join gold_item i on i.id = p.item_id"
                          " join message m on m.id = i.message_id where p.run_id = %s", (res["run_id"],)):
        preds.setdefault("email" if r["medium"] == "email" else "teams", []).append(r)
    by = {k: {} for k in preds}
    for k, rows in preds.items():
        for r in rows:
            by[k].setdefault(r["field"], []).append(r)
    # Teams: origin person, fixed; the question survives the gate (origin person) and so does the route
    for r in by["teams"]["origin"]:
        assert (r["top"], r["selected"], r["decided"], r["scores"]) == ("person", ["person"], True, {"person": 1.0})
        assert r["raw"]["fixed"] == "person"
    assert all(r["selected"] == ["question"] and "gate" not in r["raw"] for r in by["teams"]["ask"])
    assert all(r["selected"] == ["Network"] for r in by["teams"]["route"])
    assert all(r["top"] == "conversation" for r in by["teams"]["type"])
    # email: marketing mail about shopping: both gates fire, and raw says so and what they changed
    for r in by["email"]["ask"]:
        assert r["top"] == "question" and r["selected"] == [] and r["scores"]["deadline"] == 0.7
        assert r["raw"]["gate"] == ["ask:origin"] and r["raw"]["ungated"] == ["question", "deadline"]
    for r in by["email"]["route"]:
        assert r["top"] == "Costs" and r["selected"] == []
        assert r["raw"]["gate"] == ["route:topic"] and r["raw"]["ungated"] == ["Costs"]


def test_the_dry_run_of_v2_names_the_templates_and_both_question_sets(conn, ingestor, taxonomy_loaded):
    sid = _answer_key(conn, ingestor)
    res = jev_gold.run(conn, sid, "message", dry_run=True)
    text = jev_gold.format_dry_run(res)
    assert "question templates v2" in text and "(email 7, teams 6 questions)" in text
    old = jev_gold.run(conn, sid, "message", dry_run=True, template=1)
    assert old["question_version"] == jev.question_version(QUESTIONS_V1, template=1)
    assert old["question_version"] != res["question_version"]


# the reference for the v2 report: per position (origin, topic, ask, route)
V2_LABELS = {1: ("person", "Work/Security", ["question", "deadline"], ["Security"]),
             2: ("marketing", "Shopping", [], []),
             3: ("person", "Work/Company", ["action"], ["Company & Internal"]),
             4: ("auto_reply", "Work/Meetings", [], []),
             5: ("notification", "IT services", ["action"], []),
             6: ("person", "Family", [], [])}
V2_ROWS = {1: _case("person", "Work/Security", ("question", 0.8), ("Security", 0.85), deadline=0.7),
           2: _case("marketing", "Shopping", ("question", 0.75), ("Costs", 0.8)),          # both gates, right
           3: _case("person", "Work/Company", ("action", 0.6), ("IT Operations", 0.9)),   # ask undecided; route wrong
           4: _case("auto_reply", "Work/Meetings", ("none", 0.9), ("Company & Internal", 0.8)),  # auto_reply gate
           5: _case("notification", "IT services", ("action", 0.9), ("none", 0.9)),       # ask gate, wrong
           6: _case("person", "Family", ("none", 0.95), ("none", 0.95))}


def _label_v2(conn, sid):
    for pos, (o, t, a, r) in V2_LABELS.items():
        gold.save_label(conn, sid, pos, "origin", [o], labeller="claude")
        gold.save_label(conn, sid, pos, "topic", [t], labeller="claude")
        gold.save_label(conn, sid, pos, "ask", a, labeller="claude")
        gold.save_label(conn, sid, pos, "route", r, labeller="claude")
    conn.commit()


def test_the_report_recomputes_a_stored_v2_run_by_one_choice_and_the_gates(conn, ingestor, taxonomy_loaded):
    sid = _answer_key(conn, ingestor, n=6)
    _label_v2(conn, sid)
    _fake_run(conn, sid, "run-v2", "context", V2_ROWS, template=2)
    rep = jev_gold.report(conn, "run-v2")
    r = rep["runs"][0]
    assert r["template_version"] == 2
    a, rt = r["scores"]["ask"], r["scores"]["route"]
    # ask: 1 right with its deadline; 2 and 5 gated; 3 undecided → none; 4 and 6 none
    assert a["exact"] == pytest.approx(4 / 6, abs=1e-3) and (a["precision"], a["recall"]) == (1.0, 0.5)
    assert (a["any"]["tp"], a["any"]["fp"], a["any"]["fn"], a["any"]["tn"]) == (1, 0, 2, 3)
    assert rt["exact"] == pytest.approx(5 / 6, abs=1e-3) and (rt["precision"], rt["recall"]) == (0.5, 0.5)
    assert rt["per_value"]["IT Operations"] == {"tp": 0, "fp": 1, "fn": 0, "precision": 0.0, "recall": None}
    # the gates, counted and checked against the reference
    assert r["gates"] == {"ask:origin": {"fired": 2, "right": 1, "wrong": 1, "unscored": 0},
                          "route:auto_reply": {"fired": 1, "right": 1, "wrong": 0, "unscored": 0},
                          "route:topic": {"fired": 1, "right": 1, "wrong": 0, "unscored": 0}}
    for g in policy.GATES:
        assert sum(r["splits"][k]["gates"][g]["fired"] for k in jev_gold.SPLITS) == r["gates"][g]["fired"]
    # the sweep varies p and m for ask and route, never s
    sw = r["sweep"]
    assert len(sw["ask"]) == len(sw["route"]) == len(jev_gold.SWEEP_ONE)
    ask = {(pt["min"], pt["margin"]): pt for pt in sw["ask"]}
    assert ask[0.7, 0.15]["exact"] == pytest.approx(4 / 6, abs=1e-3)   # the run's own policy
    assert ask[0.5, 0.15]["exact"] == pytest.approx(5 / 6, abs=1e-3)   # 3's action at 0.6 is now selected
    assert ask[0.95, 0.4]["exact"] == pytest.approx(3 / 6, abs=1e-3)   # 1's question at 0.8 is not
    text = jev_gold.format_report(rep)
    assert "question templates v2 (ask and route as one choice, gated)" in text
    assert "ask:origin 2 (right 1, wrong 1, unscored 0)" in text
    assert "Threshold sweep, ask and route (one choice" in text and "(selected when score ≥ s)" not in text
    # the kind is read from the scores when the run did not record its templates
    conn.execute("update model_run set params = params - 'template_version' where id = 'run-v2'")
    assert jev_gold.report(conn, "run-v2")["runs"][0]["gates"] == r["gates"]


def test_the_report_keeps_scoring_a_stored_v1_run_as_statements_without_gates(conn, ingestor, taxonomy_loaded):
    sid = _answer_key(conn, ingestor, n=6)
    _label_v2(conn, sid)
    v1 = {pos: {"origin": c["origin"], "topic": c["topic"],
                "ask": {"question": 0.8, "action": 0.6, "decision": 0.1, "deadline": 0.1, "my_commitment": 0.1},
                "route": {"Security": 0.7, "IT Operations": 0.9, "Costs": 0.1}} for pos, c in V2_ROWS.items()}
    _fake_run(conn, sid, "run-v1", "context", v1, template=1)
    r = jev_gold.report(conn, "run-v1")["runs"][0]
    assert r["template_version"] == 1 and r["gates"] == {}
    a = r["scores"]["ask"]
    assert a["any"] == {**a["any"], "tp": 3, "fp": 3}           # yes to everything, the gated ones too
    assert r["scores"]["route"]["per_value"]["IT Operations"]["fp"] == 6
    ask = {pt["min"]: pt for pt in r["sweep"]["ask"]}
    assert set(ask) == set(jev_gold.SWEEP_MANY) and all("margin" not in pt for pt in r["sweep"]["ask"])
    assert ask[0.5]["any_precision"] == 0.5 and ask[0.9]["any_recall"] == 0.0
    assert "(selected when score ≥ s)" in jev_gold.format_report(jev_gold.report(conn, "run-v1"))


def test_a_v1_run_and_a_v2_run_are_compared_per_field_per_medium_with_the_gate_counts(conn, ingestor, taxonomy_loaded):
    sid = _answer_key(conn, ingestor, n=6)
    _label_v2(conn, sid)
    v1 = {pos: {**c, "ask": {"question": 0.8, "action": 0.1, "deadline": 0.1}, "route": {"IT Operations": 0.9}}
          for pos, c in V2_ROWS.items()}
    _fake_run(conn, sid, "run-v1", "context", v1)       # from before template_version was stored
    _fake_run(conn, sid, "run-v2", "context", V2_ROWS, template=2)
    rep = jev_gold.report(conn, "run-v2", compare_run="run-v1")
    assert [r["template_version"] for r in rep["runs"]] == [2, 1]
    text = jev_gold.format_report(rep)
    assert "Side by side: context (run-v2)  vs  context (run-v1)" in text
    assert "context v2   context v1" in text
    for split in ("email", "teams", "all"):
        assert f"  {split} (" in text
    head = text.index("Side by side")
    assert "    ask     precision" in text[head:] and "    route   exact set" in text[head:]
    allsplit = text[text.index("  all (6 items)"):]
    assert "    ask     any: precision               100.0%        50.0%" in allsplit   # v1 said yes to every item
    assert "    route   precision                     50.0%         0.0%" in allsplit
    assert "    gate    ask:origin              2 (1 wrong)            —" in text[head:]
    assert "    gate    route:topic             1 (0 wrong)            —" in text[head:]
