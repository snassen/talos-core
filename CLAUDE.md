# Working in Talos

Talos is a local command center over the owner's own mail and Teams. Read
`docs/architecture.md` first: it holds the layer model and the decisions behind it.
This file holds the rules for changing the code. The working practice around them (the loop, UI and
backend conventions, services, pitfalls, what needs the owner's go) is the skill `talos-dev` in
`.agents/skills/talos-dev/` (linked into ~/.claude/skills and ~/.codex/skills).

**The owner's own rules come first.** When `AGENTS.md` exists in the personal part (`uv run talos config path`
prints the folder, usually `~/TalosData/config`), read it before changing anything: it holds the hard lines
for their instance, on top of the ones here.

## Commands

```bash
uv run pytest -q                    # all tests; needs PostgreSQL on port 5433 (creates talos_test)
uv run talos setup                  # create/migrate the real database and seed the accounts
uv run talos status                 # what is synced, and whether each secret is present
uv run talos serve                  # UI on http://127.0.0.1:7420

# UI work against invented data, never the real archive:
export TALOS_DSN="host=/tmp port=5433 dbname=talos_demo" TALOS_HOME=/tmp/talos-demo
uv run python scripts/demo.py && uv run talos serve --port 7421
```

PostgreSQL 18 runs as a Homebrew service on **port 5433**. Port 5432 is left to any other PostgreSQL on
the Mac: never touch it.

## The promises (held by tests/test_guards.py)

1. **Sends only what the owner composed and confirmed.** Talos sends a mail (or a Teams post) only when the owner
   presses Send on a mail they composed, after confirming the sending account. No AI, rule, job or
   automation ever sends mail, and no API endpoint can send without a fresh human confirmation.
   With several accounts, a default account may be set, but no mail goes without confirming the
   account it goes from. (Before, the promise was "never send mail".) Held by: SMTP and Graph sendMail exist only
   in `talos/send.py`; only the web app's send route imports it (no CLI command, sync, rule run,
   Argus timer or model step can reach it); the Mail.Send scope is named only in `graphauth.py`
   and `send.py`; and a send needs a one-time token that the compose pane's confirm step issues
   for that exact message (account, recipients, subject, text), valid five minutes, used once.
   **Teams posts** follow the same promise: a post in a chat, a new post in a channel or a reply in a channel
   thread goes only when the owner presses Enter (or Send) in that conversation's own box. That keypress is the
   confirmation, since the box belongs to one conversation; there is no separate step. The same module and route
   pattern hold it, with a one-time token for exactly that target and text that the page redeems at once, and
   Teams's own hourly limit; the
   ChannelMessage.Send / ChatMessage.Send scopes are named only in `graphauth.py` and `send.py`. Plain text; no
   mentions, edits or deletions.
2. **Never delete permanently.** No expunge, no permanentDelete. Trash is the furthest any
   operation goes, and the vault keeps the original regardless.
3. **Sync never writes.** IMAP folders are selected `readonly=True`; bodies are fetched with
   `BODY.PEEK[]`; Graph is only sent GET. Writing to a server happens only in changeset
   executors, only for a committed changeset, and only for an account with
   `writeback_enabled`.
4. **Originals are immutable.** The vault is content-addressed and write-once.
5. **Models propose, people decide.** Model output lands as `proposed` assignments and becomes
   active only through `cases.accept` or a human decision.
6. **Mail is untrusted input.** The UI inserts data as text only (no innerHTML); attachments
   are served under a sandbox CSP; the server listens on 127.0.0.1 and checks the Host header.
7. **Secrets live in the Keychain.** Never in files, environment variables, logs or chat. Never
   read a secret just to check it exists (`secrets.exists` does not prompt).

If a change needs one of these relaxed, stop and ask the owner.

## How changes are made

- **Deterministic first.** Mechanics are code and rules. A model answers a bounded question
  over a compact record, through `talos.cases`.
- **Every assignment says where it came from:** human, rule, model or import. Human beats
  rule beats accepted model beats import (`effective_assignment`). A message's value also
  counts its thread's assignments (`effective_message_assignment`).
- **A fix comes with a test that fails without it.** Test names are sentences.
- **Transactions carry cursors.** A sync adapter commits a batch of messages together with the
  cursor that covers them; after a rollback call `Ingestor.reset_caches()`.
- **Schema changes are new files** in `src/talos/sql/`, applied in name order. There is no
  down-migration: the vault can rebuild everything.
- **Bump `PARSER_VERSION` or an extractor's `version`** when their output changes; history is
  then re-derived from the vault (`talos reparse` for the parser).
- **Test mail is invented** (`tests/mailfactory.py`). Real mail never enters the repo;
  `.gitignore` excludes `.eml`/`.emlx` outside tests.

## Layout

```
src/talos/
  sql/            schema migrations
  config.py       where Talos keeps things: TALOS_HOME, TALOS_DSN, the tailnet allow-list (web.json)
  personal.py     the owner's own configuration, outside the repository: TALOS_CONFIG or TALOS_HOME/config (accounts,
                  the Jev recipient text, structure, rules, discovery drafts), else the examples in rules/; init
  where.py        talos where: the code, the personal part, the data, the database, the Keychain (names only),
                  the services and the backups, each with why it lives there
  backup.py       the nightly backup of the owner's own work (talos backup; TALOS_HOME/backups/<date>), restore in
                  docs/backup.md
  db.py           connections and migrations (applied in name order, recorded in schema_migration)
  secrets.py      the macOS Keychain, and nowhere else; exists() never prompts
  accounts.py     the owner's accounts and addresses (from TALOS_HOME/config/accounts.json), and the sync source for each
  graphauth.py    Microsoft sign-in (device code): read scopes for sync, each write scope asked for on its own
  vault.py        L0: content-addressed originals
  mime.py         raw RFC 822 → ParsedMessage (pure)
  textclean.py    HTML to text, quote stripping (Swedish and English), snippets
  extract.py      PDF, Excel and photo metadata
  ingest.py       L0 → L1: rows, people, orgs, threads, edges, search vector
  reparse.py      re-derive text, snippets and attachment flags after a parser change
  teams_ingest.py Teams Graph JSON (vault kind 'json') → the same rows, people and search as mail
  sources/        read-only sync adapters: gmail, graph, imap, local, teams, and the calendars (m365calendar,
                  icloudcalendar, googlecalendar); base: cursors, run bookkeeping, the IMAP fetch helpers
  rules.py        conditions as data (ANDed; {"any": [...]} OR groups; 'in' lists), compiled to SQL; human
                  assignments; remove (values go, the definition stays in rule_removed); load_file (idempotent)
  suggest.py      rule suggestions: drafted from accepted values (20+ agreeing messages per sender or subject
                  pattern, no conflict), always off; a drill-down (field → category → value → suggestions, with
                  counts and coverage); a ticked value or category becomes one OR rule per value (group_rule);
                  a removed rule's suggestions come back; the web app caches the drafting and coverage
  objects.py      meta-objects: rule, manual and query membership, exclusions, nesting, common()
  activity.py     a binder's activity log (work, mail, notes), its facts and its notes
  events.py       machine mail → versioned events
  importance.py   the importance score: signals with their points, the owner's override, Today's lists
  sureness.py     how sure a value is, in words: Fact, Certain, Confident, Likely, Maybe, Doubtful, Guess (level_of,
                  level_sql, the sure= filter's floor)
  studio.py       Tune › Studio: cards of grouped mail Jev is unsure of (pattern or thread), the owner's decisions on the
                  whole group with undo, verdicts per cell (field, value, level, side), lifts of a cell's proposals once
                  the owner's verdicts prove it, relift of new mail after each sync; docs/studio.md
  enrich.py       enrichment step A: automated reasons, subject patterns, sender profiles, origin by rules, coverage report
  taxonomy.py     the closed value lists (rules/taxonomy.json) into the dimensions: talos taxonomy load
  gold.py         enrichment step B: the answer key (frozen stratified sample, blind labels, reveal, scores;
                  labellers: Claude's labels imported, the owner's check of a sample, agreement; per-set
                  fields; unsure sets with sender groups, every labeller against the owner, the owner's answer given to
                  a group)
  changesets.py   L3: plan → commit → apply → undo
  structure.py    the mailbox structure planner: rules/structure.json → structure_plan (where every mail should
                  go under Talos/), its report, "why here", Gmail changesets made planned, never committed, and
                  the "how to make it real" checklist with each step's live state
  writeback/      L3 executors (Gmail IMAP, Graph); built only for writeback_enabled accounts
  cases.py        the model step as files (export records, validate decisions, accept); from before Jev
  jev.py          step C, the Jev adapter (outside the core): the TypeSafe client (key from the Keychain,
                  read lazily), questions from the taxonomy (templates v2: ask and route as one choice, a
                  Teams-window set; v1 still buildable), masked case records in two unit designs
  mask.py         masking before anything leaves the Mac: personal ID, card, IBAN, phone numbers, URL paths
  policy.py       the routing policy: Jev's probabilities → decided values (versioned thresholds, cross-field gates)
  jev_gold.py     Jev on the answer key: runs (resume, cost guard, dry run), jev_case/jev_prediction, the report
  backfill.py     Jev over the archive in collapsed units (template samples, threads, Teams windows) per stage:
                  enrich_case/enrich_prediction, proposals propagated to messages (the runs chain: newer
                  supersedes older), accept/unaccept by policy, two-level (boundaries first, then exact values)
  boundary.py     the four boundaries (sender_kind, sphere, form, keep): sides from the taxonomy's families,
                  a side's probability the sum of Jev's full scores; kind, the level above type, the same way
  focus.py        focused Jev runs: the questions a case is unsure of (uncertain, disagree, all), one field or
                  several combined in one request per case; the answer key first (--gold)
  incremental.py  Jev for new mail (talos enrich jev new, and the sync hook when enrich.json turns it on): what is
                  unjudged, reuse of a template's, thread's or window's answers, the rest asked (full set, then
                  sender_kind, kind and value), accepted by the stored policy (kind from type where the direct kind is
                  unsure), placed again; throttle, daily budget, talos-enrich check-in
  acceptance.py   the acceptance explorer: answer-key cases and archive histograms per field, examples, apply
  compose.py      writing mail: drafts, the sending accounts and why one cannot send, signatures (per account,
                  new or reply, one default each), mismatch warnings, the MIME message. Never sends
  send.py         the only code that sends: confirmation tokens, SMTP and Graph transports, Teams posts, the rate
                  limits (20 mails, 120 Teams posts an hour), send_log (never the body). Imported by web/app.py alone, for its two send routes
  unlock.py       Tune › Jobs & fruit: low-hanging fruit (sender groups of undecided mail; an answer key of them, Claude's blind
                  export, apply to the groups) and improvement jobs (focused runs per field, done/dismissed)
  clusters.py     Tune › Clusters: machine mail by sender and system, dead/quiet/active, planned cleanup
  discovery.py    Tune › Systems: the drafts in TALOS_HOME/config/discovery (systems, candidates, the owner's setup) loaded
                  for review (talos discovery load; never overwrites a decision), the owner's accept/reject, the binders accepting
                  makes (an existing binder of the same name is enriched, not duplicated), export back to the files
  calendars.py    the calendar: Talos's own calendars and entries, and copies of the Microsoft 365, iCloud and Google
                  calendars (sync_source over the read-only sources/m365calendar, icloudcalendar (CalDAV),
                  googlecalendar); docs/calendar.md
  calwrite.py     writing entries the owner saves to their real calendars (Graph, CalDAV, Google), never with attendees, never a
                  series, trash at most; imported by the web app alone
  googleauth.py   Google sign-in for the calendar (OAuth, installed app, PKCE; refresh token in the Keychain)
  work.py         work items: statuses, one home, related binders, linked mail, the history of every change
  space.py        the work space: areas with their binders, what needs the owner, watchers; binder terms and neighbours
  discover.py     Discover: deterministic insights over the archive, and saved aggregations
  timeline.py     Work › Timeline: binders and work items as a Gantt (start_on to due), the load per day (meeting
                  hours from the calendar, items running and due); docs/timeline.md
  insight.py      computed answers in insight_cache: read by fingerprint, recomputed behind the page
  manual.py       the live manual: docs/manual.md read into the sections Talos Web's ⓘ buttons open
  releases.py     CHANGELOG.md read into data, for the Release notes page and talos --version
  search.py       search, message detail, conversations (threads) and overview, shared by CLI and web
  mailhtml.py     a message's HTML for the reading pane: allow-list cleaner, cid: pictures, remote images
                  blocked unless asked, the frame's CSP (shown in <iframe sandbox>, never in the page)
  facets.py       senders, type/topic/label counts and top people over the same filters as search
  export.py       Parquet snapshots for DuckDB; JSONL rows for AI
  argus.py        Argus, the service monitor: check-ins, probes, status, macOS notices, the outbound heartbeat
                  to a hosted dead-man's switch (its one egress; URL in the Keychain); docs/argus.md
  obsidian.py     lift an Obsidian vault into objects, work items and notes (talos vault import)
  syncnow.py      Sync now (mail, as its own process) and TeamsFast, Teams's 20-second lane
  webauth.py      signing in to Talos Web: the owner's password and an authenticator code (TOTP), sessions, lockout
  web/            Starlette API + vanilla JS UI (no build step); gate.py: the door (every route needs a session,
                  the host check, the bulk-read guard)
  cli.py          the talos command
tests/            pytest; fakes for Gmail IMAP and Graph that fail on any write; smtpfake.py, a loopback SMTP
                  server that delivers nothing (no test sends a real mail)
scripts/demo.py   invented demo data for UI work
```
