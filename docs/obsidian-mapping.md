# An Obsidian work vault → Talos Web: mapping

Status: proposal. **Nothing is built yet.** The owner decides the open questions at the end first.

## What the vault is

The proposal assumes an Obsidian vault (kept in iCloud Drive; inspected read-only) that is a
work-and-decision system with its own contract (`_system/Governance/talos-contract.md`).
The rules that matter here:

- **Markdown files hold state.** Bases, Canvas and Home are projections. Every work item has
  one canonical file, and one permanent **home**: a project, area, topic or system binder.
- **It summarises and links to source systems; it does not replace their history.** Mail and
  chat stay where they are, and a work item points at them (`source-kind`, `source-account`,
  `source-id`, `source-link`).
- **Agents act as "driver" only when handed the role.** Every filing carries `routed-by`,
  `routed-at` and a routing receipt. The owner's corrections always win.
- **No new live integrations without the owner's separate approval** ("do not introduce workers,
  routers, hooks, or live integrations without a separately approved change"). The only
  approved machinery is a display plugin (display only, plus Inbox-board filing) and a
  watch-only history layer.
- It must stay usable on iPhone and iPad (iCloud), and without an agent.

What such a vault holds, for example:

| | Example |
|---|---|
| Projects | a handful, plus a personal one and an archived one |
| Areas | Security, Costs, Devices & Hardware, IT Operations … |
| Topics | AI Tooling, Remote Access |
| Systems | Check Point, Azure, M365, UniFi, a NAS …, each with a State note |
| Work items | dozens, spread over next, inbox, done, someday, blocked, doing and todo |
| Signals | Calendar, Teams, Long Jobs |
| Bases (views) | a kanban Board per binder, grouped by `status`, and a few overall views |
| Canvases | mostly a small three-card "Purpose / Current context / Next decision" per binder |
| Routing receipts | from the agents and the owner |

**The link to Talos Web already exists.** Work items made from mail in a mail client such as Spark
carry a `source-link` token that encodes the message's Message-ID, and most of them resolve to
exactly one message in Talos Web. Teams-sourced items carry a chat source that can be matched the
same way.

## The question this decides: which system owns the work

The mail archive and the work system are different kinds of thing:
- Talos Web is the competent source system for *communication history*: every message, its
  people, its search.
- The vault is where *work and decisions* live, deliberately small, portable and usable
  from the phone.

Three ways to join them:

| | A. The vault stays the source; Talos Web reads it (recommended) | B. Talos Web becomes the source; Obsidian retires | C. Both editable, two-way sync |
|---|---|---|---|
| Phone and iPad | Keeps working (Obsidian, iCloud) | Lost, until Talos Web is reachable from the phone | Keeps working |
| Agents already driving the vault (Claude, Codex …) | Unchanged | Must move to the Talos Web API | Unchanged, but racing Talos Web |
| The vault's contract | Respected; needs one approved change (a read integration) | Replaced | Needs a new conflict model |
| Complexity | Low: one read adapter, later a few guarded writes | High: rebuild boards, canvases and the driver flow | Highest: conflicts across iCloud, agents and web |
| What you get in Talos Web | Your projects, areas, topics and systems as objects, their boards, and each work item next to the mail and chats it came from | The same, with Talos Web as the only home | The same |

**Recommendation: A.** The vault already has the right rules, and the agents work in it. What
it lacks is what Talos Web is good at: the full mail and chat history, people, search and
importance. Joining them by reference gives both, and moves no source of truth.

## The mapping (for option A)

| Obsidian | Talos Web | Notes |
|---|---|---|
| Binder `_home.md` (`type: project` / `area` / `topic` / `system`; personal projects too) | An **object** of that kind. The object kinds get `topic` and `system` added | `uid`, path, `lifecycle`, Purpose, What belongs here and Outcome kept as attributes. `parent`, `areas` and `topics` links become edges |
| Work item (`type: work-item`) | A new **work item** entity: title, status, home, focus, due, review-after, source fields, routed-by/at, receipt, file path | The status vocabulary is kept exactly: inbox, next, doing, blocked, someday, done |
| `source-link` / `source-id` on a work item | An **edge from the work item to the message or chat** | Resolved by Message-ID (Spark token) or chat id. Unresolved ones are listed, never guessed |
| `home` | The work item's membership of its binder object | One home, as the contract says |
| `related` | Secondary membership edges | |
| Binder `work.base` (a kanban by status) | A **Board** view on the object page in Talos Web | Read-only in phase 1 |
| `Views/attention.base` (inbox, doing, blocked, focus, overdue) | A **Needs attention** widget on the Overview | Sits next to "Waiting for your answer" |
| `Views/operations-exceptions.base`, System State notes | Health, update and security flags on system objects; an **Operations** widget | Unknown stays unknown, never "healthy" |
| Inbox (`status: inbox`, `suggested-home`, `inbox-reason`) | An **Inbox** list in Talos Web | Filing stays in Obsidian in phase 1 |
| Signals (Calendar, Teams, Long Jobs) | Shown as signal cards; Long Jobs next to Events | |
| Canvas | Not mapped in phase 1. Each object gets an **Open in Obsidian** link (`obsidian://open?vault=<vault>&file=…`) | They are maps. Rendering them read-only is possible later |
| Routing receipts, History change feed | Provenance on each work item ("filed by claude, receipt …") | |

## How it would work

- **A read-only vault source** in Talos Web, like the mail sources. Every 5 minutes it reads
  the Markdown files that changed since the last run (by modification time) and updates the
  objects, work items and links.
  - iCloud placeholder files (not downloaded) are skipped and reported, never treated as
    deleted.
  - A file that fails to parse is listed and left alone.
- **Talos Web never writes to the vault in phase 1.** Nothing about the vault changes; the
  contract still needs one approved line for the read integration.
- **In Talos Web:** object pages show the binder's board and its work items, and each work
  item opens the mail or chat it came from. Messages show "part of work item X in project Y".
  The Overview gets the Needs attention and Operations widgets.

## Phases

| Phase | What | Writes to the vault |
|---|---|---|
| 1 | Read the vault: binders become objects, work items link to messages, Board, Inbox and attention views | None |
| 2 | **"Make a work item" from a message** in Talos Web: creates one Markdown file in `Inbox/Items/`, following the vault's template and vocabulary, `status: inbox`, `routed-by: <owner>`, with a receipt | One file per click, only in Inbox |
| 3 | Board moves in Talos Web change a work item's `status`: compare before writing, never overwrite a file changing on another device, receipt per change | Frontmatter `status` only |
| 4 | Maybe: canvases rendered read-only; binder creation from Talos Web | Later decision |

Phases 2 and 3 each need a separate yes, and each is also a change to the vault's contract.

## Decisions for the owner

1. **Which option: A (recommended), B or C?**
2. **Approve Talos Web as a read integration of the vault?** It is phase 1, and a new "live
   integration" in the vault contract's terms. The exact contract line would be proposed, for
   the owner to add or approve.
3. **Add PyYAML** to parse frontmatter reliably? A small, standard library. The alternative is
   a hand-written parser for the vault's simple YAML, which is more fragile.
4. **Later:** are phases 2 and 3 of interest? And should work items also link to Talos Web
   (`http://127.0.0.1:7420/#…`) alongside the mail client?
5. **Another task board:** if an earlier plan made a separate kanban the only task ledger, the
   vault's work items would make a third place for tasks. Does the vault replace it?
