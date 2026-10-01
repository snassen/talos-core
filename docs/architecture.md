# Talos 2.0: architecture

Status: agreed direction, 2026-09-23; phase P0 is built (see README). It records the decisions made with the owner and
the reasons for them. Anything marked **open** still needs the owner's call.

## What Talos is

A local command center over the owner's own communication. It ingests every
message from their mail accounts (and later Teams), keeps the originals untouched,
and builds light, fast objects on top of them. The owner can sort, label, group, link and
analyse those objects in ways no mail server supports. A small, fixed set of
changes (read, flag, archive, move, trash and labels) can be pushed back to the
servers in reviewed batches, so the mail client on their phone stays in step.

A separate mail client (for example Spark) remains the mail client. Talos sends a mail only when the owner
presses Send on a mail they composed in it and confirms the sending account; nothing
automated ever sends. It never permanently deletes anything.

## Principles

1. **Originals are immutable.** Every raw message is stored exactly as fetched and
   never rewritten. Everything else can be rebuilt from it.
2. **Deterministic first.** Mechanics are code and rules: parsing, threading,
   identity, classification rules, extraction, planning and writing. Models such as
   Jev answer bounded questions over compact records. Their answers are stored as
   proposals with provenance, and never act on a mailbox by themselves.
3. **Small objects, rich links.** A message row holds what a list or a rule needs.
   Bodies, attachments and raw files live elsewhere and are read only on demand.
4. **Every assignment says where it came from:** human, rule, model or import.
   A human decision always wins, and a rule can be re-run without disturbing one.
5. **Batches, not clicks, touch the server.** Changes to mailboxes are planned,
   reviewed and then committed as a changeset that runs in the background, with
   a per-message log and an undo record.
6. **Sends only on the owner's confirmed Send, never destroys**, and a test fails the build if
   code that could send elsewhere, or destroy, appears (CLAUDE.md, promise 1).
7. **Local and private.** Everything runs on this Mac (FileVault). The web server
   listens on 127.0.0.1 only. Secrets live in the macOS Keychain.

## The layers

```
L0  Vault        ~/TalosData/vault — raw .eml (zstd) and attachments, content-addressed by SHA-256
L1  Components   PostgreSQL — message, attachment, person, org, thread, participants, server state, text + search
L2  Objects      PostgreSQL — entity graph: objects (meta-objects), edges, dimensions + assignments, events
L3  Write-back   PostgreSQL — changesets and their operations; executors per provider
```

### L0: the vault

- `vault/raw/ab/cd/<sha256>.eml.zst`: the complete RFC 822 message, compressed
  with zstd, which is lossless and in Python's standard library since 3.14. The
  hash is taken over the *uncompressed* bytes, so it identifies the message.
- `vault/att/ab/cd/<sha256>`: each attachment decoded, stored once however many
  messages carry it. The raw message still contains it too, so the original stays
  complete. Storage is cheap; losing an original is not.
- Writes are atomic (temp file, fsync, rename). A blob is never overwritten.
- The vault comes out at roughly the size of the mail itself, since attachments
  compress poorly but are deduplicated.

### L1: components

One row per **message per account**, keyed by a stable provider key:
Gmail's `X-GM-MSGID`, Graph's immutable ID, or for plain IMAP the Message-ID plus
content hash. The row holds headers that matter, the direction (in, out, self), the
thread, a snippet and flags such as `has_attachments` and `is_automated`.

- `message_text` is kept separately: the cleaned body, the body with quoted replies
  removed, and one search vector built from **both the Swedish and English
  dictionaries**, so that "fakturor" finds "faktura" and "invoices" finds "invoice".
- `message_location` mirrors the server's state: folder, UID, flags and labels or
  categories, plus whether the message is still present. Planning a changeset
  compares against this mirror.
- `participant` stores every from, to, cc, bcc and reply-to address with its role.
- `person`, `org` and `address` are built deterministically from headers. An org is
  the sender's domain, except for free-mail domains such as gmail.com.
- `attachment` holds the metadata, the blob hash and extracted text: PDF text,
  Excel cells, and photo EXIF (date, camera).
  A part counts as an attachment (`has_attachments`) when it has a filename or
  `disposition=attachment`, except an image the HTML shows by `cid:` inside
  multipart/related and the calendar alternative of an invitation. A forwarded message
  (`message/rfc822`) is stored as its original bytes, and its text is added to the
  forwarding message's body, marked, so a search finds it.
- Every message row records the `parser_version` that produced it. When the parser
  improves, `talos reparse` re-reads older messages from the vault and refreshes their
  text, search vector, snippet and attachment flag, in committed batches; people,
  threads and existing attachment rows keep their ids.

### L2: objects and the graph

Everything addressable is an **entity**: a message, attachment, person, org,
thread, event or meta-object. Entities share one ID space, so a single `edge` table
can link anything to anything:

| Edge | Meaning |
|---|---|
| `message —in_thread→ thread` | the thread it belongs to |
| `message —from→ person`, `—to→`, `—cc→` | participants |
| `message —has_attachment→ attachment` | the file it carries |
| `person —works_at→ org` | inferred from the address domain |
| `entity —member_of→ object` | membership of a meta-object |
| `entity —excluded_from→ object` | a manual exception to a rule-made membership |
| `message —same_message→ message` | the same mail seen in two accounts |

Identical files in different messages need no edge: they share a blob hash, which is
indexed.

Edges carry their `source` (`ingest`, `rule:<id>@<version>`, `human` or
`model:<run>`), so rule-made links can be recomputed and human ones are never lost.
Graph questions, such as "which people and organisations do these 40 threads have
in common", are recursive SQL over `edge`. At this size no graph database is needed;
the graph can be exported to a visual tool when exploring.

**Dimensions and assignments** replace the Gmail limit of one label per mail. A
dimension is a named field: `topic`, `type`, `action`, `area`, `tag` and so on. It
can hold one value (topic) or many (tag). An assignment gives an entity a value, and
records:

- `source_kind`: human, rule, model or import
- `source_ref`: the rule and its version, or the model run
- `status`: active, proposed, rejected or superseded
- `confidence`, and `evidence` (why)

The **effective value** is resolved deterministically: an active human assignment
wins, then a rule, then an accepted model proposal, then an import. Model output
arrives as `proposed` and only becomes active when accepted, whether one at a time
or in bulk through a policy the owner approves.

Values are ranked per entity (`effective_assignment`), but the actors work at different
levels: rules and the owner's own decisions usually assign **messages**, while cases (Jev) and
thread-level decisions assign **threads**. So a message's value is resolved over both,
in `effective_message_assignment`: the message's own active assignments and its
thread's compete together, human beats rule beats accepted model beats import across
both, and within a tier the message's own assignment beats the thread's, then the
newest wins. For a many-value dimension every active value of either counts, once.
Each row says `via` message or thread. A human decision on a thread therefore beats a
rule's value on its messages. Search filters, the message drawer and the
`message_values` export use this view; anything about an entity itself (a thread's
case record, the `assignments` export) keeps using `effective_assignment`.

**Meta-objects** are `object` rows: a project, case, collection, life area or saved
search. Their members come from three places:
1. **Rules** (recomputable).
2. **Manual** additions and exclusions (kept).
3. **A stored query**, whose membership is evaluated live.

Objects can contain objects, and an entity can belong to any number of them.

Draft examples, of the kind drawn from the owner's own labels and folders, to correct rather than accept:

| Meta-object | Contents | How it forms |
|---|---|---|
| **Receipts to report — September** | Receipts in a "Receipts to report" folder with PDFs attached, amount and date extracted | Rule on folder and month; the action dimension moves `to_report` → `reported` |
| **Insurance** (life area) | Mail from insurers, policy PDFs, renewal dates, the insurers as orgs | Rule on sender orgs + Gmail label "Insurance"; manual additions |
| **Customer / supplier X** | Every thread, attachment, person and task involving their domain | Automatic per org; promoted to a project by hand |
| **Backup health** | All backup reports as events, success or failure per job and day | Event extractor; a dashboard, not a list |
| **A work project** | Threads, Teams messages, documents and tasks about a rollout | Manual + keyword rules; later Teams channels |

**Events** are how machine mail becomes useful. Much of a work
mailbox is notifications, backup reports, alarms and alerts. Each known sender type
gets a small, versioned extractor that turns a message into an `event` row, such as
"backup job X, failed, 23 Sep 02:14". Events feed dashboards and trends; they don't
clutter the object views. Extractors are re-runnable, so improving one re-reads
history.

### L3: write-back through changesets

A **changeset** is a reviewed batch of server changes:

1. **Select:** a search, a rule, an object, or a hand-picked set.
2. **Plan:** Talos computes one operation per message and account against the
   mirrored server state, skips no-ops, and shows the counts per account, folder and
   operation.
3. **Commit:** the owner approves it.
4. **Apply:** a background worker runs the operations in chunks, grouped for
   batching. Gmail's IMAP can relabel or move thousands of messages per command, and
   Graph takes requests in batches of 20. It checkpoints after each chunk, logs every
   message's result, stores the inverse operation, and is safe to resume.
5. **Reconcile:** the next sync confirms the server state. If the server disagrees
   (say the mail was already moved in Spark), the server wins and the changeset
   shows the discrepancy.

The operations are: `mark_read`, `mark_unread`, `flag`, `unflag`, `archive`, `move`,
`trash`, `add_label` and `remove_label`. They map to providers like this:

| Operation | Gmail (IMAP + Gmail extensions) | M365 (Graph) | Plain IMAP |
|---|---|---|---|
| read, flag | `\Seen`, `\Flagged` | `isRead`, `flag` | `\Seen`, `\Flagged` |
| archive | remove the `\Inbox` label | move to Archive | move to Archive |
| move | change labels | move to folder | `MOVE` |
| trash | move to Trash | move to Deleted Items | move to Trash |
| add/remove label | `X-GM-LABELS` under `Talos/…` | categories under `Talos/…` | keywords where the server allows |

Talos never expunges and never calls a permanent delete. **Write-back is off by
default** and is switched on per account once the read-only phase has proven the
sync.

The executors live in `talos/writeback/` and are built only for accounts whose
settings say `writeback_enabled`. Gmail writes go through All Mail, opened read-write
only there, after checking UIDVALIDITY and that each UID still holds the expected
X-GM-MSGID; trash is a `COPY` into Trash with no `\Deleted`, `MOVE`, `CLOSE` or
expunge. Graph writes are `PATCH` and `/move` inside JSON batches of 20, and need
`Mail.ReadWrite` (`graphauth.WRITE_SCOPES`); the work account's
write-back is switched on for one agreed use at a time and off afterwards (docs/writeback-test-plan.md).
`talos changeset apply ID` runs a committed changeset, or says why it will not.

## Sync

Every 5 minutes via launchd (not installed yet), one run per account:

| Account | Method | Sign-in | Cursor |
|---|---|---|---|
| Gmail | IMAP on All Mail (`\All`), read-only select, `BODY.PEEK` | App password in Keychain | UIDVALIDITY + last UID; flag changes via CONDSTORE |
| Work (Microsoft 365) | Microsoft Graph mail delta per folder, immutable IDs, MIME via `$value` | Entra public client, device code; token cache in Keychain | deltaLink per folder |
| iCloud, other IMAP | Plain IMAP per folder | App password / password in Keychain | UIDVALIDITY + last UID per folder |
| Teams (account `teams`, seeded off) | Graph chats and channel messages; originals are the message JSON | the work account's sign-in | per chat: last modified + resume link; per channel: backfill link, then delta |

A cursor is saved **in the same transaction** as the messages it covers, so a crash
never skips or duplicates mail. Backfill runs in checkpointed batches; the first
full pass over a large archive (100k+ messages) takes hours, which is fine overnight. Throttling
(`429` with `Retry-After`) is honoured with a per-account gate.
Messages deleted on the server are marked `present = false`; their originals stay in
the vault.

No message is silently lost, and no single message stops an account:

- IMAP searches new mail with `UID last+1:*`; the full UID list (one line, so
  `imaplib._MAXLINE` is raised to 64 MB) serves gone-detection and a **catch-up**:
  UIDs at or below the cursor that Talos neither holds nor lists in `ingest_failure`
  are fetched again on every run. Bodies come in batches bounded by `RFC822.SIZE`
  (50 MB); a UID left out of a FETCH is asked for alone, and otherwise listed in
  `fetch_failure` until it arrives or leaves the server.
- Graph: a message deleted before its `$value` download (404) is skipped; any other
  download error goes to `fetch_failure` and is retried at the start of the next run.
  An expired delta token (410, `syncStateNotFound`) restarts the folder, and what the
  new pass does not see is marked gone.
- `message_location.folder` is the server's key (Graph folder id, IMAP folder name,
  `[all]`); `folder_path` is the readable path that rules match. A renamed Graph folder
  updates `folder_path`; a vanished one has its locations marked gone. Hidden folders
  are skipped unless `include_hidden_folders` is set, and `skip_folders` skips subfolders too.

Sent versus received: Gmail's `\Sent` label, Graph's Sent Items folder, and in every
case the From address checked against a list of all the owner's own addresses.

## Rules

Rules are **data**: ordered, a list of conditions
ANDed together, and an action (assign a dimension value, add to an object, or mark
for an event extractor). Conditions cover sender address and domain, recipients,
subject, List-Id, folder, provider label, attachment type, automated flag, date and
body text. Each rule is compiled to one SQL query, so re-running a rule over 100k+
messages takes seconds, and a **preview** (how many messages, which ones) is free.

Logic that doesn't fit conditions is a Python rule function, registered and
versioned the same way. The existing Gmail labels and Outlook folders are read as
server state that rules can use. Taxonomy v2 from the Codex calibration seeds the
`topic` dimension.

## The model step (Jev)

Unchanged from the Codex findings, and simpler here because the records come
straight from the database:

1. Deterministic code builds **case records** per thread: IDs, dates, senders,
   subject, current assignments, and up to 1,800 characters of cleaned text.
2. The records go to Jev (or another model) as JSONL in bounded batches.
3. The decisions come back as JSONL. They are validated for exact ID coverage and
   allowed values, then stored as `proposed` assignments with the run as their source.
4. Spam holds and uncertain cases stay unresolved. Nothing reaches a mailbox except
   through a changeset the owner commits.

The model runner is an adapter outside the core, because the models live elsewhere
(for example the Codex CLI).

## Search, analysis and AI rows

- **Search:** PostgreSQL full text with Swedish and English dictionaries, trigram
  matching for names and subjects, and filters on any dimension, object or field.
  A search never reads the whole archive: the matching ids come first, each branch
  from its own index (GIN on the search vector; trigram GIN on subject and sender
  address), and are then joined for filters, ranking and paging. A dimension filter
  starts from the assignment value index and checks each candidate's effective value.
  PostgreSQL's Swedish stemmer is conservative (kvitto and kvitton stay apart), so plain
  words also match as prefixes trimmed by two letters: "kvitton" searches `kvitt:*`.
- **Facets:** the Messages sender sidebar and its Type, Topic and Label dropdowns are grouped
  counts over the same matches as the list (`search.messages_sql` in its `select` form, in
  `talos.facets`), each leaving out its own filter. Type and topic rank each message's own and
  its thread's values exactly as `effective_message_assignment` does, but over the selection
  only: the view answers one message at a time and would rank the whole archive for a set.
- **Analysis:** export to Parquet and query with DuckDB, for heavy aggregation
  without loading the live database.
- **AI rows:** clean JSONL per thread (quote-stripped text, participants,
  assignments), ready for any model.
- **Semantic search** ("mails like this"): pgvector is installed; embeddings come
  in a later phase.

## Technology

| Part | Choice | Why |
|---|---|---|
| Pipeline, rules, sync, API | Python 3.14 | Readable, scriptable, strong mail libraries |
| Database | PostgreSQL 18 on port 5433 | Swedish full-text search, many concurrent writers, JSONB, pgvector, ready for a VM later |
| Analysis | DuckDB + Parquet | Fast columnar crunching on exported snapshots |
| Web | Starlette + a vanilla JavaScript UI | No framework, no build step for now |
| Secrets | macOS Keychain via `keyring` | Never in files or environment variables |
| Scheduling | launchd | Native; nothing extra to run |

Libraries: `psycopg`, `imapclient`, `msal`, `httpx`, `keyring`, `selectolax`,
`pypdf`, `openpyxl`, `pillow`, `duckdb`, `pyarrow`, `starlette`, `uvicorn`, `pytest`.

## Phases

| Phase | Content | Server writes |
|---|---|---|
| **P0 Foundation** | Vault, schema, parser, ingest of `.eml`, search, rules, changeset model, guard tests, CLI, web shell | None |
| **P1 Read-only sync** | Gmail All Mail + M365 Graph backfill and 5-minute incremental sync | None |
| **P2 Objects** | People and orgs, dimensions, taxonomy v2, event extractors, meta-objects, the mockup views on real data | None |
| **P3 Write-back** | Changeset executors for Gmail and Graph, switched on per account after review | Reviewed batches only |
| **P4 Teams and models** | Teams chats and channels, calendar context, Jev case records, AI exports, embeddings | None new |
| **Later** | Log analytics for relay mailboxes and system logs; move to a VM | — |

## Open decisions

- Whether Talos or another task board owns tasks, and whether tasks sync to Microsoft To Do.
- Which Talos dimensions, if any, are mirrored to Gmail labels or Outlook categories
  (the proposal: none until the owner picks, and then only under `Talos/…`).
- Retention: whether mail deleted on the server should ever leave the vault.
- Company mail on a personal Mac: acceptable only if the person responsible for security agrees; to be
  written down as a decision.
