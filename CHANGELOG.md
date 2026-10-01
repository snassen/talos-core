# Release notes

What changed in Talos, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the version numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html): MAJOR.MINOR.PATCH, where
a new minor version adds features and a patch only fixes. Before 1.0, anything may still change.

Each change goes under **Unreleased** in the same commit that makes it; a release moves them under a new version
(`.agents/skills/talos-dev/scripts/release.py`). Talos Web shows these notes under the version number in the rail.

## [Unreleased]

### Added
- **`talos doctor`**: is this Mac ready for Talos, and the next step for anything missing, with step-by-step
  guides (Gmail, Microsoft 365, PostgreSQL, your personal part, the services…). It is talos-doctor, its own
  product in its own repository; Talos runs it, from its repository when it is not installed. It reads only.
- **`talos doctor pr OWNER/REPO NUMBER`**: screens a pull request for text aimed at a model before an agent
  reads it (talos-doctor's screen): block, review or clean, and a cleaned view an agent may read.
- **The screen lab** (`talos screen`): samples of prompt injection and of ordinary text, to measure and train
  talos-doctor's screen. Each source is a module, switched on or off with its own cap, pinned to the revision it
  was imported at: deepset prompt-injections, LLMail-Inject (Microsoft), agent-injection-bench,
  AgentInjectionBench, Talos's own code as ordinary text, and the repository-files dataset (gated: needs a
  Hugging Face token). Duplicates across all sources, exact or near, are found and left out. A rules run is free;
  a Jev run is estimated first and needs a cost limit. The report gives catch and false-alarm rates per source,
  kind of text and rule, and the rules against Jev; it never shows a sample.
- **An MIT license** (LICENSE).
- **Before you start** (README): what Talos gives you, what it is not, and what you need to bring.

## [0.14.0] - 2026-10-01

### Added
- **Teams: a boost after you post.** For two minutes after a post, Talos looks for the answer every 5 seconds
  instead of every 20, where you posted (the chats, or that channel), on whichever page you are; each post
  starts the two minutes again, and then it cools down by itself.
- **A nightly backup of your own work**: the first full sync after 02:00 writes TALOS_HOME/backups/<date>/ with
  a dump of the tables only you can make (binders, work items, notes, rules, answer keys, Studio decisions,
  changesets, calendars, drafts and settings), every value you set keyed by the message's vault hash so it can
  be given back even to a database rebuilt from the vault, and the links you made. Fourteen days are kept,
  never fewer than three. `talos backup` makes one by hand, `--status` shows the latest; Argus watches it as
  talos-backup. docs/backup.md says how to restore.
- **`talos where`**: where everything is, and why: the code, your personal part (yours, or the examples in
  use), the data folder, the database (with how many of your own decisions and work items live only there),
  the Keychain items by name (never read), the background services and the backups. `--write FILE` saves it
  as Markdown.
- **`talos config init`** makes your personal part from the examples and never overwrites a file. The part can
  live anywhere: `TALOS_CONFIG` names it, for example a private git repository of your own.
- **Installing Talos** (docs/install.md): the code and the personal part set up apart, the personal part either
  in a private repository or only on your Mac; `talos launchd` now carries `TALOS_CONFIG`, `TALOS_HOME` and
  `TALOS_DSN` into the printed jobs when they are set.
- **More of you lives in your personal part**, so the code holds no trace of any one owner: `owner.json` (the
  name your decisions are stored under, your name in what Jev reads, your services' label prefix),
  `questions.json` (your own wording of the questions Jev is asked), `taxonomy.json` (your own value lists),
  `argus-services.json` (your other services for Argus to watch) and `AGENTS.md` (your rules for coding
  agents, which they read first). Each account in `accounts.json` may carry `jev_label`, `sphere` and `ui`
  (its name, letter and colour in Talos Web). Talos reads them; without them it uses the examples.
- **CONTRIBUTING.md and SECURITY.md**: how to work on Talos, and how to report a vulnerability privately.
- **`/api/owner`**: the page takes the account names, letters and colours, and your id, from your
  configuration instead of having them written into it.

### Changed
- The repository speaks of "the owner" and "you": the examples, the tests, the demo and the docs use invented
  people, accounts and machines, and `tests/test_traces.py` fails if a trace of a real owner comes back.
- Columns that record who did something no longer default to a name; the code always names the owner
  (migration 032, no stored value changes).

### Changed
- **Teams: Enter sends.** In a conversation, Enter posts what you wrote at once (Shift + Enter for a new line);
  the separate "Yes, post to …" step is gone, as the box belongs to that one conversation. A new channel post
  sends with ⌘/Ctrl + Enter or Send. What you are writing stays in the box while new messages come in round it.
- **Teams has its own hourly limit**: 120 posts an hour, apart from the 20 mails an hour, so chatting never uses
  up the mail limit.

## [0.13.1] - 2026-10-01

### Security
- **No personal data in the repository.** Your accounts and their Microsoft IDs, the paragraph Jev is told
  about you, your mailbox structure, your researched rules and the discovery drafts mined from your mail now
  live in `~/TalosData/config`, and the reports in `~/TalosData/docs`. The repository keeps examples. Talos
  reads exactly what it read before; the tests can no longer touch your data folder.

### Fixed
- `talos auth google` failed at once (the Google calendar sign-in could not start).
- The logs no longer grow without end: `talos.log` is moved aside at 10 MB (five kept), and the service logs
  (`launchd.err` and the like) hold warnings and errors only, instead of a second copy of everything.
- Several texts said write-back to the work account waited for a permission given on a set date; they now
  give the real reason it is planned only there (it is switched on per agreed use).

### Removed
- Leftovers: an unused duplicate of the events route, unused functions and constants, and about 70 lines of
  styles for pages that no longer exist.

## [0.13.0] - 2026-10-01

### Changed
- **Kind is decided for many more messages, free.** Where Jev's direct answer on kind stayed unsure, Talos now
  uses the kind worked out from Jev's answer on type, which the answer key shows is right more often (94.6% against
  89.3% at the same level). Nothing new is asked of Jev; `talos enrich accept` does it, for new mail too.
- **New mail is asked people or machine directly**, as the archive was in the combined run. New mail was decided
  less often (86% against 96%). It adds a fraction of a cent a day.
- **✓ Done** is red, and the **ⓘ** buttons that open the manual are now in the accent colour, so they are easy to
  see.

## [0.12.0] - 2026-10-01

### Added
- **A red chip for mail that has left the mailbox**: *Purged* (moved to Deleted Items by a Talos cleanup, such as a
  prune), *Deleted* (moved there outside Talos) or *Not on server*, on every row in Mail, in the
  message pane (with which cleanup moved it, and when) and on Studio cards.
- **✓ Done** beside a work item's status: marks it done and saves in one click.

### Changed
- **The Studio shows the mail you still have first**: groups rank by their unsure mail still in your folders, with
  the last 90 days counting double. Groups already purged come last, marked *all removed*. Each card says how many
  of its group are still in your folders.
- **A Studio decision updates the mailbox structure plan at once**: the planned `Talos/…` folders follow your answer
  (and its undo) straight away, instead of at the next full plan.

## [0.11.0] - 2026-09-30

### Added
- **Studio** (Tune › Classify › Studio): like naming faces in a photo library. One card at a time shows a group of
  mail Jev is unsure of (a system's same notice, or a conversation), with Jev's guess ticked for people or machine,
  kind, topic and value. `Enter` confirms, a number or typed letters tick another value, `0` says *not this*. Your
  answer goes on the whole group (or just the message shown), and a counter shows how many messages you settled.
  One card can settle thousands of messages. Skip and undo are one key each.
- **Lifts**: every line you confirm or correct also checks Jev. When your yeses show that one kind of guess (say,
  Jev's *alert* at Maybe on machine mail) is right often enough, all Jev's other guesses of that kind are accepted
  at once, and new mail's after each sync. Each lift can be undone.
- **How sure**: every value now carries a level in words: Fact (you decided), Certain, Confident, Likely, Maybe,
  Doubtful and Guess. It shows beside each value in the message pane, and Mail has a **Sureness** filter: *Certain
  or surer* keeps only the surest values, and from *Likely* down Jev's unaccepted guesses come in too.

### Changed
- **Faster pages.** Talos now fetches the main places (Mail, Work, Calendar, Teams, Today) in the background shortly
  after it opens, and a place in the rail while you point at it, so their first visit is instant (Mail from 0.8 s to
  about 50 ms). It learns what each page needs as you use it. A message's, a conversation's or an attachment's own
  contents are never fetched ahead, so the read limit is untouched.
- **Insights and Discover's saved groupings** are kept on the server and open in about 40 ms instead of 0.5–0.6 s.
  When new mail has come, they show what they had and count again behind the page, then update once. A grouping you
  add or remove shows at once.
- A change you make (moving a card, marking a mail) no longer makes every page slow again: the page you are on
  shows the change at once, and the others show what they had and update themselves a moment later.

### Fixed
- The Structure page downloaded 26.7 MB each time it opened (every changeset's full dry run); it is now 87 kB.

### Changed
- **Controls you can find.** A message's actions sit in outlined groups with a small title (Reply, Work, Importance,
  Conversation, Binders, More), every action has a symbol before its word, and Reply, Reply all and Forward come
  first in their own colours: Reply red, Reply all violet, Forward green. Important has a red exclamation mark, a
  question you have not answered an amber question mark, Not important a grey line.
- **Number keys for the places**: 1 Today, 2 Mail, 3 Teams, 4 Work, 5 Calendar, 6 Binders, 7 Discover, 8 Tune, each
  shown beside its place in the rail. "[" and "]" step through a place's tabs (Board, List, Timeline …), and "/" opens
  search. Not while you type in a field.
- **Shortcut letters** for a message: R reply, A reply all, F forward, W make a work item, L link to one, I important,
  N not important, C open the conversation, B add to a binder. Each button shows its letter.
- Nothing hides until you point at it: the menus on board cards and watchers are always in view, and a watcher shows
  Take in and Seen. Quiet buttons have an outline instead of bare grey text. Appearance › Controls › Quiet brings back
  the lighter look.

### Added
- **Teams, a place of its own.** Teams left Mail and has its own place in the rail (under More on the phone), with
  its own filters: both directions by default, and a switch for One-to-one, Group chats, Meetings or Channels.
  Every row says its kind (1:1, Group, Meeting, # channel). The sidebar lists the chats by kind and your teams,
  each opening to its channels.
- **A found message opens where it was said.** In Teams, a message found by a search or a filter opens its
  conversation with that message in the middle, marked, with Load earlier and Load later.
- **Posting in Teams.** Write under a conversation to post in that chat or reply in that channel thread, or start
  a new post from ✎ beside a channel. Send shows where it goes, in large type, with your text; only "Yes, post to
  …" posts it. Plain text; nothing automated ever posts.
- **Near-instant Teams, all the time.** A small background service looks for new chat messages every 20 seconds
  (about two seconds each time), day and night; the Teams page shows what arrives within about ten seconds of that.
  A channel is read again at once after you post in it. Argus watches the service (Teams fast lane).

### Changed
- **Mail is e-mail only**, and opens on received mail in every account; the Teams chip is gone from it.

### Added
- **Your other calendars, live.** The Calendar now shows your iCloud calendars (the family ones, your own, and the
  subscribed ones) beside Talos's and Microsoft 365's, and Google's once you have signed in (docs/calendar.md says
  how). It copies them every five minutes, when you press Refresh, and by itself when you open it on an older copy.
- **Writing back.** Create or edit an entry in a real calendar and it is saved there at once: iCloud now, Microsoft
  365 once the Calendars.ReadWrite permission is granted, Google once you have signed in. New entries can go to any
  calendar Talos can write. Removing an Outlook or Google entry sends it to their trash.
- **Teams channels are read**: the teams you are in and their channels are listed and their messages
  copied, now that Team.ReadBasic.All and Channel.ReadBasic.All are granted. Until now only chats were read.
- **Alerts**: an entry in a real calendar can have an alert (at the start, minutes, hours or days before), which
  that calendar fires on your phone and computer.

### Changed
- Entries with invitees, occurrences of a repeating series and other people's meetings stay read-only in Talos,
  and say why: changing them there would send updates to people. Talos never invites anyone.

### Added
- **More colour**: everything with a colour of its own now carries a faint wash of it. Binders their kind (the
  area cards, the chips, a binder's page and the list), work cards their home, board columns their status, the
  rows on Today the reason they need you, next meetings their calendar, the sources their account, and mail rows
  a trace of their account. Light themes mix the colour into the surface; dark themes lift the surface a little
  towards it. Appearance › Colour sets it: Off, Subtle (the default) or More.

### Added
- **The manual, inside Talos.** A small ⓘ beside a page's title, or a card, opens the part of the manual that
  describes it: the field guide, rewritten for the pages as they are now. It opens over the page,
  which dims and stays where it was; click outside, the ×, or Esc to close it. "See also" moves to related parts,
  and Back returns. The ⓘ is only where the manual has something to say: every place, and things like the account
  chips, the sender sidebar, a message's values and importance, Watchers, Found in your mail and Neighbours. The
  text lives in `docs/manual.md`, so it can be corrected when a page changes.

### Changed
- **A new mark: a beacon.** A lighthouse keeping watch, as Talos is often shown, in the rail and on the browser
  tab. The tower is in the text colour; its light tells you how things stand: the accent when all is calm,
  amber when there is important mail today, a review due or a service late, red when work is overdue or a service
  is down. Point at it to see why; on hover the light turns. The browser tab shows the beacon too, always blue.
  The mark is larger than before, and the word Talos is in the text colour. The app icon (the phone's home screen,
  and what Safari shows in its tabs) is the beacon too.

## [0.10.0] - 2026-09-28

### Added
- **All Jev jobs in one run.** The improvement jobs under Jobs & fruit overlap a lot: a message Jev is unsure of
  kind is often unsure of topic and route too. A new job at the top of the list asks each of those cases once, with
  only the questions it is unsure of, in one request (`talos enrich jev focus --field kind,topic,… --where
  uncertain`). On a large archive: about 60% fewer cases, about a quarter fewer tokens and 2.5 times fewer
  requests. Its answers count exactly as the separate runs' would.
- **The answer key checks several questions at once** (`--field a,b,… --gold`): one request per item, each field
  scored beside the full question set and the field asked alone. It opens the gate for every field in it.

## [0.9.0] - 2026-09-28

### Added
- **A new design** (docs/design.md). Six places instead of 18 rail entries: **Today**, **Mail**, **Work**,
  **Calendar**, **Binders** and **Discover**, plus **Tune** for the tools (answer key, acceptance, rules,
  structure, changesets, sources, clusters, systems, Argus) as tabs. Places with several pages show them as tabs
  at the top; each opens on the tab you used last. Every old link still works.
- **Today**: where the day starts. What needs you in one list (overdue, blocked, review due, in focus, doing, the
  inbox, and watchers with news), the next meetings, who is waiting for your answer, today's most important mail,
  the services' health in one line, and your areas. The rail shows how many things need you.
- **Appearance** (at the foot of the rail): six themes (Talos, Slate, Paper, Graphite, Midnight, Bronze), each
  light and dark; your own accent colour; text size; sans or serif headings; comfortable or compact rows; square,
  soft or round corners; how accounts are marked; and full, reduced or no motion. It changes the look only, shows
  the contrast of the text as you choose, and is kept in this browser.
- **Account letters**: every row shows its account as a letter in the account's colour (for example W for a work
  account, G Gmail, i iCloud, T Teams), so accounts are told apart by letter as well as colour. The account chips
  show the same letters. Appearance can switch back to a thin bar, or a bar and a tinted row.
- **Search** at the top of the rail, and ⌘K (Ctrl+K) anywhere, opens Mail with the search field ready.
- **On the phone**: a tab bar at the bottom (Today, Mail, Work, Calendar, More); Mail's filters in one Filters
  sheet; the board one column at a time; the sender sidebar starts closed.
- **Movement where it helps**: the reading pane slides in from its edge, menus and messages fade in, mail that
  arrived since you last looked is briefly highlighted, and loading shows grey lines instead of a word.
- **Events** shows 50 at a time, filtered by status and kind (it was one very long page).
- **Microsoft 365 dry run**: a changeset for the work account (Microsoft 365) can be dry-run. It reads each message on the server
  (read-only) and shows exactly what apply would send, without sending anything.
- **The steps of a changeset** on the Changesets page: created, planned, dry run, committed, applied, checked on
  the server, and when Talos's copy caught up, each with its time. Apply shows its timing (seconds, batches,
  throttling) and the check shows where the messages are on the server now.
- `talos changeset check`, `reconcile` and `retry`, and `create --ids-file` for long selections.

### Changed
- **Mail**: the filters are chips on one line: a set filter is filled and says what it keeps, with × to clear it.
  The search has its own line. Drafts and Signatures moved: Drafts is a quiet button, signatures open from the
  Drafts pane. Rows no longer carry a coloured wash (Appearance can bring it back).
- **Work**: wider columns; cards show the title, the home binder and only the chips that need a look; the move
  menu hides behind ⋯ until you hover or focus a card. Someday and Done fold to a narrow strip (click to open).
- **Areas** (was Work space): the map and the watchers; watchers are one row each, with Take in in view and the
  rest behind ⋯. Needs you moved to Today. Objects is now **All binders**.
- **Insights** (was the Overview's lower half, under Discover): check this, projects, top people and domains,
  events, and the optional widgets. Low-hanging fruit and Improvement jobs moved to Tune › Jobs & fruit.
- **Calendar**: the all-day row shows two entries a day and "+N more", instead of a clipped scroll.
- **A calmer look**: seven text sizes instead of 23, three corner sizes, binder kinds as a small coloured square
  beside a neutral name, red kept for overdue, down and failed (importance is blue now), and grey text dark
  enough to read easily (it was below the accessibility minimum).

### Changed
- **Teams has its own direction in Mail.** The Received / Sent filter now narrows mail only; Teams shows both sides of
  every chat, your own lines included, since a chat is both. While Teams is switched on, a Teams chip narrows it on
  its own if you want (Teams received or Teams sent).

### Fixed
- Mail opened from the rail, the phone's tab bar or Search now always starts from its default view (received mail, every
  account but Teams), instead of keeping the filters of the last detour. Watchers, senders and threads still open it
  on their own selection.
- Write-back to Microsoft 365 failed after the first 20 messages (the write sign-in worked only once), and a
  failed batch made the batches before it look failed too.

## [0.8.0] - 2026-09-28

### Added
- **Calendar** (under Work): day, work week and week views, a Today button, and the current time as a red line.
  The calendars in the sidebar are the legend: each has its own colour, a tick to show or hide it, and one is the
  default for new entries. Click an empty slot or New entry to make an entry; click it to edit it (title, calendar,
  all day, start and end, where, show as, notes), or remove it (with Undo).
- Your **Microsoft 365 calendars** (your own, shared ones, holidays, birthdays) are shown read-only, copied with the
  5-minute sync or at once with Refresh. An entry opens with its time, place, organiser, people, and links to join
  in Teams or open in Outlook. Talos does not change them; that would need your go first.
- **Timeline** (under Work): a Gantt over your binders. Each binder is a row with its span, and its work items are
  bars from start to due (a diamond with only a due date). Above them the load: meeting hours per day from your
  calendars (red from 6 hours) and the items running, to see when too much is on at once. Filter by kind, binder,
  focus and done; zoom Weeks, Months or Year. Click a binder to give it its own start and end.
- Work items have a **Start** date beside Due. In a work item, **Make a calendar entry** splits a part of it off into
  the calendar; the entry stays linked to the item and shows as a dot on its row in the Timeline.

## [0.7.0] - 2026-09-27

### Added
- **Release notes**: the version is shown at the bottom of the rail (with a dot when something is new), and opens
  this page. `talos --version` says it too. Versions follow Semantic Versioning, and every release is tagged in git.

### Changed
- **Messages filters**: kind, origin, type, topic and label moved from the top bar into the sidebar, as panels
  under the senders with counts and bars; each panel folds away and shows its top values until "Show all".
  Work/Personal and Keep became buttons in the top bar. A chosen value also shows as a chip at the top. The
  sender list shows its top 8 until "Show all", so the panels are in view.
- The sidebar button and tab say "sidebar" and "Senders & filters", since the sidebar holds both.

## [0.6.0] - 2026-09-27

### Added
- **Work space**: every area with its projects, systems and topics, what needs you, and watchers.
- **Watchers**: saved searches that count what arrived since you last looked, and take it into a binder in one click.
- **Discover**: people waiting for your answer, yearly renewals ahead, organisations without a binder, systems gone
  quiet, new people; saved aggregations.
- A binder's page finds its mail by search terms ("Found in your mail") and shows its neighbours.
- **Sync mail now** on Messages, Sources and the Overview.
- Account chips can **solo** an account, like a mixer.
- **Signing in** to Talos Web: a password and an authenticator code, sessions that end, a lock after wrong attempts,
  and a pause when a session reads a lot of mail at once. Sources › Security shows who is signed in.
- The talos-dev skill: how Claude Code and Codex work on Talos, kept true by a test.

### Changed
- The kanban is called **Board** in the rail.
- The Overview keeps its scroll position when it refreshes.

### Fixed
- Mail from one of your addresses to another shows under Received and Sent.
- The sender sidebar folds back to the top 20, and comes back after it is closed.

### Security
- The database accepts only your own macOS user; the data folder is private to your account.

## [0.5.0] - 2026-09-26

### Added
- **Compose and send**: write a new mail, reply or forward; it goes only after you confirm the account. Drafts and
  signatures.
- **Discovery**: review the drafted systems, candidate binders and your setup; accepted items become binders.
- The Overview's **low-hanging fruit** and **improvement jobs**, and the **Clusters** page for machine mail.
- **Rules v2**: removing a rule returns its suggestions; suggestions as a drill-down with a tick at any level; twenty
  researched rules.
- **Incremental enrichment**: new mail is judged shortly after it arrives, within a daily budget.

### Changed
- Changesets have a history with status colours; the Structure page explains itself.
- The "important per day" widget is gone.

### Fixed
- Gmail sync after a large backlog of label changes (searched window by window).
- Answers labelled by anyone but you count as model values, never as your decisions.

## [0.4.0] - 2026-09-26

### Added
- **Enrichment with Jev**: the hosted model on the answer key and over the archive, as proposals.
- **Two-level acceptance** and the **Acceptance** page: the boundaries first, then exact values.
- **Argus**: a monitor for the services on the Mac, with notifications and an outbound heartbeat.
- **The mailbox structure planner** and the **Structure** page.
- **Kind**, a coarse level above type.
- Mail shown as sandboxed HTML, with remote images blocked until you allow them; the Talos icon set.

### Removed
- Thirty-two seed rules that copied old labels into topics and types.

## [0.3.0] - 2026-09-25

### Added
- **Write-back** guard rails: a size limit on commits, a read-only dry run, undo, and the **Changesets** page.
- Talos Web over **Tailscale**, for your own tailnet login only.
- **Account chips**, hidden senders and domains, and rows tinted by account in Messages.
- **Binder pages** with kind colours, an About section, notes and an activity log.
- The **reading pane** beside the list, and **Conversations** (threads and Teams chats).
- **Enrichment step A** (origin by rules, sender profiles) and **the answer key** (blind labelling), with the
  **taxonomy** of value lists.

## [0.2.0] - 2026-09-24

### Added
- **Importance**: a score for every message, with its reasons.
- Messages gets the **sender sidebar** and type, topic and label filters; rules can be edited in the app.
- The **Overview** as an importance dashboard.
- **Work items** and the **Board**; the Obsidian vault lifted in (`talos vault import`).

## [0.1.0] - 2026-09-23

### Added
- The archive: every message kept as it arrived, parsed and searchable in Swedish and English.
- Read-only sync from Gmail, Microsoft 365, IMAP servers and Teams.
- Rules, events from machine mail, changesets and exports.
- The `talos` command and the first Talos Web.

[Unreleased]: ../../compare/v0.14.0...HEAD
[0.14.0]: ../../compare/v0.13.1...v0.14.0
[0.13.1]: ../../compare/v0.13.0...v0.13.1
[0.13.0]: ../../compare/v0.12.0...v0.13.0
[0.12.0]: ../../compare/v0.11.0...v0.12.0
[0.11.0]: ../../compare/v0.10.0...v0.11.0
[0.10.0]: ../../compare/v0.9.0...v0.10.0
[0.9.0]: ../../compare/v0.8.0...v0.9.0
[0.8.0]: ../../compare/v0.7.0...v0.8.0
[0.7.0]: ../../compare/v0.6.0...v0.7.0
[0.6.0]: ../../compare/v0.5.0...v0.6.0
[0.5.0]: ../../compare/v0.4.0...v0.5.0
[0.4.0]: ../../compare/v0.3.0...v0.4.0
[0.3.0]: ../../compare/v0.2.0...v0.3.0
[0.2.0]: ../../compare/v0.1.0...v0.2.0
[0.1.0]: ../../releases/tag/v0.1.0
