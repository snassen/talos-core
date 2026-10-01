"""An invented Obsidian "Talos" vault for the importer tests. Nothing here is real.

It mirrors the real vault's shape: binders with `_home.md`, Items, Notes, a system with
State, an Inbox, routing receipts, an Archive, template folders, Bases, canvases, signals,
the trial log, the History feed and an iCloud placeholder.
"""

from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import quote

MSGID = "CAHarbour-4711.cutover@mail.test.invalid"
TEAMS_CHAT = "19:aaaa1111-2222-3333-4444-555566667777_bbbb@unq.gbl.spaces"
TEAMS_MSG = "1790000000001"


def spark_link(account: str, message_id: str, n: int = 1234567890) -> str:
    """A Spark deep link as Spark writes it: base64 wrapped at 64 characters with CRLF, URL-encoded."""
    token = base64.b64encode(f"A:{account};ID:{message_id};{n}".encode()).decode()
    wrapped = "\r\n".join(token[i:i + 64] for i in range(0, len(token), 64))
    return "https://sparkmailapp.com/dpl/bl?token=" + quote(wrapped, safe="")


def binder(kind: str, name: str, uid: str, *, lifecycle="active", parent="", areas=(), topics=(), body="",
           extra="") -> str:
    def lst(xs):
        return " []" if not xs else "".join(f'\n  - "{x}"' for x in xs)
    return (f"---\ntype: {kind}\nname: {name}\nuid: {uid}\nlifecycle: {lifecycle}\nparent: {parent}\n"
            f"areas:{lst(areas)}\ntopics:{lst(topics)}\n{extra}created: 2026-09-20\n---\n\n# {name}\n\n{body}")


def item(uid: str, title: str, *, status="next", home="", related=(), suggested="", reason="", kind="manual",
         account="", source_id="", link="", due="", receipt="", created="2026-09-21T10:00:00Z", body="") -> str:
    rel = " []" if not related else "".join(f'\n  - "{x}"' for x in related)
    lines = [
        "---", "type: work-item", f"uid: {uid}", f"status: {status}", f"home: {home and repr_link(home)}",
        f"related:{rel}", "focus: false", f"suggested-home: {suggested and repr_link(suggested)}",
    ]
    if reason:
        lines.append(f'inbox-reason: "{reason}"')
    lines += [f"source-kind: {kind}", f"source-account: {account}", f'source-id: "{source_id}"',
              f'source-link: "{link}"' if link else "source-link:", "review-after:", f"due: {due}",
              "routed-by: claude", 'routed-at: "2026-09-21T10:05:00Z"',
              f'route-receipt: "{receipt}"' if receipt else "route-receipt:", f'created: "{created}"', "---", "",
              f"# {title}", "", body or "## Desired outcome\n\nSomething is decided.\n"]
    return "\n".join(lines)


def repr_link(x: str) -> str:
    return f'"{x}"'


SECURITY = "[[Operations/Security/_home|Security]]"
BACKUPS = "[[Work/Topics/Backups/_home|Backups]]"
HARBOUR = "[[Work/Projects/Harbour Migration/_home|Harbour Migration]]"
NAS = "[[Operations/Systems/Nimbus NAS/_home|Nimbus NAS]]"
RECEIPT = "[[_system/Routing/Receipts/2026-09-21 Filing|2026-09-21 Filing]]"
CANVAS = '{"nodes":[{"id":"purpose","type":"text","text":"## Purpose","x":0,"y":0,"width":300,"height":180}],"edges":[]}'


def build(root: Path) -> Path:
    files = {
        ".obsidian/app.json": "{}",
        "Home.md": "# Talos\n\n![[Views/attention.base#Home]]\n",
        "CLAUDE.md": "# Claude entry point\n",
        "Views/all-work.base": "views: []\n",
        "_system/History/changes.jsonl": '{"path": "x"}\n',
        "_system/History/Changes.md": "---\ntype: system-note\n---\n\n# Talos changes\n",
        "_system/Research/2026-09-19 Inspiration.md": "---\ntype: research-note\ncreated: 2026-09-19\n---\n\n# Inspiration\n\nIdeas.\n",
        "Trial Log.md": "# Talos trial log\n\n| Date | Test |\n|---|---|\n| 2026-09-20 | Filing |\n",
        "Signals/Teams/_home.md": "# Teams\n\n![[current]]\n",
        "Signals/Teams/current.md": ("---\ntype: observation\nuid: 20260920T100000Z-observation-teams\nkind: teams-chats\n"
                                     "state: current\nobserved-at: \"2026-09-20T10:00:00Z\"\nsource-link:\n---\n\n"
                                     "# Teams summary\n\nNothing urgent.\n"),

        # A project with Items, Notes, a canvas, a Base, a template folder and a placeholder.
        "Work/Projects/Harbour Migration/_home.md": binder(
            "project", "Harbour Migration", "20260920T100000Z-project-harbour", areas=[SECURITY], topics=[BACKUPS],
            body=("## Purpose\n\nMove the file shares to the new harbour storage.\n\n## What belongs here\n\n"
                  "The cutover.\n\n## What does not belong here\n\nOther storage.\n\n## Outcome\n\nShares moved.\n\n"
                  "## Current context\n\nCutover planned for October.\n\n## Work\n\n"
                  "![[Work/Projects/Harbour Migration/work.base#Board]]\n")),
        "Work/Projects/Harbour Migration/overview.canvas": CANVAS,
        "Work/Projects/Harbour Migration/work.base": "views: []\n",
        "Work/Projects/Harbour Migration/_templates/Item.md": "---\ntype: work-item\nuid:\nstatus: inbox\n---\n",
        "Work/Projects/Harbour Migration/Items/Confirm the cutover date.md": item(
            "20260921T100000Z-item-cutover", "Confirm the cutover date", home=HARBOUR, related=[NAS], kind="email",
            account="owner@company.example", source_id="4711",
            link=spark_link("owner@company.example", MSGID), due="2026-10-01", receipt=RECEIPT,
            body="## Desired outcome\n\nA date.\n\n## Context\n\nThe vendor proposed two dates.\n"),
        "Work/Projects/Harbour Migration/Items/Evaluate the copy tool.md": item(
            "20260921T100100Z-item-copy-tool", "Evaluate the copy tool", status="todo", home=HARBOUR),
        "Work/Projects/Harbour Migration/Notes/Kickoff decisions.md": (
            "---\ntype: decision\ncreated: 2026-09-20\n---\n\n# Kickoff decisions\n\nWe copy, then switch.\n"),
        "Work/Projects/Harbour Migration/Notes/.Budget.md.icloud": "bplist00",

        # An area and a topic.
        "Operations/Security/_home.md": binder(
            "area", "Security", "20260920T100000Z-area-security",
            body="## Purpose\n\nKeep things safe.\n\n## Work\n\n![[Operations/Security/work.base#Board]]\n"),
        "Operations/Security/Items/Rotate the shared FTPS password.md": item(
            "20260921T100200Z-item-ftps", "Rotate the shared FTPS password", home=SECURITY, kind="teams-chat",
            account="owner@company.example", source_id=f"{TEAMS_CHAT}/{TEAMS_MSG}"),
        "Work/Topics/Backups/_home.md": binder(
            "topic", "Backups", "20260920T100000Z-topic-backups", areas=[SECURITY],
            extra=f'projects:\n  - "{HARBOUR}"\n', body="## Purpose\n\nBackups in general.\n"),

        # A system with State, Sources and an incident; and the systems template.
        "Operations/Systems/Nimbus NAS/_home.md": binder(
            "system", "Nimbus NAS", "20260920T100000Z-system-nimbus", areas=[SECURITY],
            body=("## Purpose\n\nThe office NAS.\n\n## Health\n\n![[State/current]]\n\n## Authoritative sources\n\n"
                  "![[Sources]]\n\n## Work\n\n![[Operations/Systems/Nimbus NAS/work.base#Board]]\n")),
        "Operations/Systems/Nimbus NAS/State/current.md": (
            f'---\ntype: system-state\nsystem: "{NAS}"\nhealth: degraded\nversion: "7.2"\nupdate-available: true\n'
            'security-findings:\nobserved-at: "2026-09-20T09:00:00Z"\nsource-link:\n---\n\n# Current state\n\nOne disk warns.\n'),
        "Operations/Systems/Nimbus NAS/State/recent.md": "# Recent state\n\n- 2026-09-20: disk 3 warns.\n",
        "Operations/Systems/Nimbus NAS/Sources.md": "# Authoritative sources\n\n- The NAS admin page.\n",
        "Operations/Systems/Nimbus NAS/Incidents/2026-09-01 Disk failure.md": "# Disk failure\n\nDisk 2 replaced.\n",
        "Operations/Systems/_template/_home.md": binder("system", "", ""),
        "Operations/Systems/_template/State/current.md": "---\ntype: system-state\nhealth: unknown\n---\n",

        # A personal project and an archived project.
        "Work/Personal Projects/Sourdough/_home.md": binder(
            "project", "Sourdough", "20260920T100000Z-project-sourdough", body="## Purpose\n\nBread.\n"),
        "Archive/Work/Old Pilot/_home.md": binder(
            "project", "Old Pilot", "20260901T100000Z-project-old-pilot", lifecycle="archived",
            body="## Purpose\n\n## Work\n\n![[Archive/Work/Old Pilot/work.base#Board]]\n"),
        "Archive/README.md": "# Archive\n",

        # The Inbox.
        "Inbox/_home.md": "# Inbox\n\n```talos-triage\n```\n",
        "Inbox/Items/Answer the landlord about the keys.md": item(
            "20260922T100000Z-item-landlord", "Answer the landlord about the keys", status="inbox",
            suggested=SECURITY, reason="A guess: keys are physical security.", kind="email",
            account="owner@company.example", source_id="4712",
            link=spark_link("owner@company.example", "not-in-talos@mail.test.invalid")),
        "Inbox/Items/Book the owners meeting.md": item(
            "20260922T100100Z-item-owners", "Book the owners meeting", status="inbox", kind="meeting",
            account="owner@company.example", source_id="4713",
            link=spark_link("personal-team", "0415073c-acf2-4893-90a6-70352f6375b@Spark")),

        # A routing receipt that names one binder.
        "_system/Routing/Receipts/2026-09-21 Filing.md": (
            "---\ntype: routing-receipt\nrun: 2026-09-21 Filing\nagent: claude\ncreated: \"2026-09-21T10:05:00Z\"\n---\n\n"
            "# Routing receipt: 2026-09-21 Filing\n\n| Item | Filed to |\n|---|---|\n"
            "| [[Work/Projects/Harbour Migration/Items/Confirm the cutover date\\|Confirm the cutover date]] "
            "| [[Work/Projects/Harbour Migration/_home\\|Harbour Migration]] |\n"),
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (root / "Work/Projects/Harbour Migration/Notes/Empty stub.md").write_bytes(b"")
    return root
