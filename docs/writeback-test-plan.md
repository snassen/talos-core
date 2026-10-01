# Write-back: the careful first test

Status: plan, and the record of the first runs. Write-back is **off** for every account. This plan switches it
on for one test message at a time, and never for the archive at large.

Why careful: a bad write-back cannot destroy mail, since Talos never expunges or sends and
trash is the furthest any operation goes. But it could move, relabel or flag thousands of
messages wrongly, and undoing that on the server is a lot of work, even with backups.

## What protects the mailboxes already

- Write-back is refused unless the account has `writeback_enabled`, the changeset is
  committed, and an executor exists. Nothing is enabled today.
- The executors can only mark read or unread, flag, archive, move, trash and add or remove
  labels. Labels are limited to `Talos/…` plus Inbox, Trash and Starred. There is no send, no
  expunge and no permanent delete; guard tests fail the build on either.
- Every operation is planned against the mirrored server state, applied in checkpointed
  chunks, logged per message, and stores its inverse for undo.

## The test, step by step (Gmail first; Graph after the same pattern)

1. **A test message.** The owner sends themselves one mail with the subject
   "Talos write-back test 1". Nothing else is ever selected.
2. **Guard rails in code, before anything runs.**
   - **A per-changeset size limit:** refuse to commit a changeset over N messages unless a
     `--large` flag is given. N starts at 1.
   - **A dry-run executor** that connects, checks the UIDs and UIDVALIDITY, reports exactly
     what it would send, and sends nothing.
3. **Enable Gmail only**, with `writeback_enabled` on the gmail account.
4. **For each operation, in this order:** add the label `Talos/test`, remove it, flag, unflag,
   mark unread, mark read, archive, un-archive (the inverse), trash, restore.
   1. Create, plan and commit a changeset selecting only the test message by id.
   2. Dry-run it and read the report.
   3. Apply it.
   4. Sync, and check that the mirrored state shows the change.
   5. Check in the Gmail web UI and in Spark by eye.
   6. Undo, sync again, and check it is back.
5. **Stop at the first surprise.** Trash on Gmail is the least certain operation, so it goes
   last, and its undo is checked separately.
6. **Microsoft 365** needs a separate consent step for `Mail.ReadWrite` in the Entra app. It
   follows only when Gmail passes, with the same test mail.
7. **Switch write-back off again** until a real use has been agreed, for example "archive the
   mails labelled 'Get rid of'". Scale up in steps: 1 → 10 → 100 → all, checking each time.

## Built for the first run

- **Size limit:** `talos changeset commit ID` refuses a changeset over 1 message.
  `--max N` raises the limit for that one commit, on purpose.
- **Dry run:** `talos changeset dry-run ID` opens Gmail read-only (EXAMINE). It checks
  UIDVALIDITY and the message's X-GM-MSGID, shows the current flags and labels, and lists
  the exact IMAP commands apply would send. It needs no write-back switch and sends nothing.
  An executor built for a dry run refuses apply.
- **Undo, list and cancel on the command line:** `talos changeset undo ID` makes the
  reversing changeset, planned. It is dry-run, committed and applied like any other.
- **Selection by id:** `talos changeset create --ids 123456 …`.
- **The Changesets view:** click a changeset to see its plan, its operations and the latest
  dry-run report, and run the dry run again.

A round, for one operation:

    talos changeset create --title "Test: add Talos/test" --op add_label --args '{"label":"Talos/test"}' --ids ID
    talos changeset plan N
    talos changeset dry-run N
    talos changeset commit N
    talos changeset apply N
    talos sync gmail            # check the mirror; then check Gmail and Spark by eye
    talos changeset undo N      # prints the new id M; dry-run, commit, apply M; sync again

## Result of the first Gmail test

All ten changesets (five operations and their undos) passed on the one test mail. Each was dry-run, applied, synced and checked in Talos, then checked by eye in Gmail
on the web and in Spark.

| # | Operation | Sent | Result |
|---|---|---|---|
| 1/2 | add/remove label `Talos/test` | `±X-GM-LABELS ("Talos/test")` | label seen in Gmail and Spark, then gone |
| 3/4 | flag/unflag | `±FLAGS (\Flagged)` | Gmail also sets and clears `\Starred`; star in Gmail, orange pin in Spark |
| 5/6 | mark unread/read | `±FLAGS (\Seen)` | as expected |
| 7/8 | archive/un-archive | `±X-GM-LABELS ("\\Inbox")` | left and returned to the inbox |
| 9/10 | trash/restore | `COPY` into the Trash folder (localised, e.g. `[Gmail]/Papperskorgen`); in Trash `+\Inbox −\Trash` | in Trash, then back with a new All Mail UID, matched to the same message by X-GM-MSGID |

Found and fixed during the test:
- The dry run printed labels unquoted. It now prints them exactly as they go on the wire.
- A restore from Trash was skipped once a sync had marked the mail gone. It now uses the
  last known location and finds the mail in Trash by X-GM-MSGID. A test covers it.

Seen, not a Talos issue:
- Spark's auto-read marked the test mail read when it was opened.
- Spark's Trash view stayed empty while the mail was in Gmail's Trash.
- The label `Talos/test` stays in Gmail's label list, empty, until deleted in Gmail.

Write-back was switched off again afterwards. Next: agree on a first real use, then
scale 1 → 10 → 100 with `--max`. Microsoft 365 follows the same pattern after `Mail.ReadWrite`
consent, and it needs a dry run first.

## Microsoft 365: a first real use (a prune)

The first real use on a Microsoft 365 account was a prune of old logs and alerts, with write-back switched on for
the run and off afterwards. It was broad because the whole account had a fine-grained backup elsewhere (a NAS's
Microsoft 365 backup). The steps grew 1 → 5 → 25 → 100 → 1,000 → thousands. The candidates were logs over 90 days,
notices and security mail over a year, dead systems first, oldest first; invoices, licences, conversations, Keep,
binders and work items were left out. The candidate list and the run log stay in the data folder
(`TALOS_HOME/prune/`), never in the repository. The work itself was a project accepted from Discovery's candidates.

### What happens, step by step

1. **create**: a changeset row: the selection (message ids) and the request (`trash`). Nothing else.
2. **plan**: one `changeset_op` per message, decided against Talos's copy of the server (`message_location`):
   pending, or skipped with a reason. Each op stores its **inverse** (for trash: move back to the folder it was in).
3. **dry run** (`writeback.dry_runners_for`): Microsoft 365 is read with the *read-only* token: a JSON batch of up
   to 20 `GET /me/messages/{id}?$select=parentFolderId…` per round trip. Each message must be on the server where
   the copy says; the report lists the exact request apply would send. Nothing is written.
4. **commit**: the owner's approval; refused over `--max N` messages.
5. **apply**: pending ops in chunks of 500. Per chunk, the Graph executor posts JSON batches of 20
   `POST /me/messages/{id}/move {"destinationId": "deleteditems"}` with the *write* token (Mail.ReadWrite),
   honours Retry-After, and records each message's result and the time. The chunk is committed to the database
   before the next one, so an interrupted apply resumes. Timing goes to `summary.apply` (seconds, per chunk,
   batches, requests, throttled, seconds waited).
6. **check** (`talos changeset check ID`): reads each applied message back from the server (read-only) and
   compares with Talos's copy. `summary.check`.
7. **sync**: the next `talos sync` (every 5 minutes, or at once) sees the moves through the folders' delta links:
   the old location goes `present = false`, the Deleted Items location appears. The message id is immutable, so it
   is the same message, not a new one, and nothing is downloaded again.
8. **undo**: a new changeset of the stored inverses; dry-run, commit and apply it like any other.

### Measured

| Step | Apply | Per message | Batches | Throttled | Sync after | Copy caught up |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 0.52 s | 515 ms | 1 | 0 | 6.8 s | 5 s after apply |
| 5 | 0.49 s | 98 ms | 1 | 0 | 5.9 s | 5 s |
| 100 | 4.5 s | 45 ms | 5 | 0 | 4.1 s | 8 s |
| 1,000 | 43.4 s | 43 ms | 50 | 0 | 11.6 s | 59–64 s (the check ran first) |
| 2,000 | 96 s | 48 ms | 101 | 2 | 3 s | ~2 min (the full check read-back was throttled: 371 s) |
| 5,000 | 540 s | 108 ms | 250 | 186 | 27 s | 7–11 min |
| 10,000 | 632–703 s | 63–70 ms | 500 | 90–147 | 36–56 s | 3–12 min |

None failed, and every dry run was clean. A quiet sync of the account (nothing new) takes a few seconds.

**The pace is Microsoft's.** From 5,000 up, the dry run (reads) and the apply (moves) both ran at about 16 messages
a second: Exchange Online's limit of about 10,000 requests per 10 minutes per mailbox. Throttled requests were
waited out as Graph asked (Retry-After), and the 5-minute sync was throttled now and then while it ran. Reading
every applied message back after apply doubled the load for little gain, so large steps check a random sample of
300 (`--sample`); Talos's copy is still compared for all of them after the sync.

A couple of the moved messages were the owner's own old sent mail: an old Exchange (X500, `IMCEAEX-…`) address
was not known as the owner's, so they counted as machine mail. They were moved back with undo changesets. List
every old address of yours in accounts.json.

### Found and fixed

- **The write token worked only once.** `_graph_token` imported `WRITE_SCOPES` inside its first-call branch, so the
  second call raised UnboundLocalError: every apply over 20 messages failed on its second batch. It had never shown,
  because every earlier test was one message.
- **A broken batch threw away the batches before it.** The 20 moves that had been carried out were recorded as
  failed. The Graph executor now keeps the results of answered batches and gives the error only to the rest.
- **The dry run caught it.** The next step's dry run found 20 messages in Deleted Items that Talos's copy still
  had in their old folder, and stopped before anything was committed.
- New: `talos changeset reconcile ID` asks the server whether failed moves happened anyway and marks those done
  (their inverse kept, so undo covers them); `talos changeset retry ID` puts the rest back to pending.

## Microsoft 365: a second use, alerts and internal-system notifications

A second run purged the notifications of internal systems, and alerts, that were no longer needed (the backup
still held them). Write-back was switched on for the run and off afterwards.

First, Jev decided more of the mail: the improvement jobs as one combined focused run (talos.focus run_many) and
`talos enrich accept --run all --two-level`. That promoted more origin `alert` and `system_report` values, which the
selection then used beside kind and type.

**The selection** (a candidate file and a review file in `TALOS_HOME/prune/`): the work account's incoming machine
mail still in a folder, any age:
- *alerts*: kind alert or report; or type backup, alarm, CI/dev, incident, maintenance, bounce, service, change,
  usage or security notice; or origin alert or system_report with no kind or fyi;
- *internal*: kind fyi or none yet, from the company's own machines (its own domain) or the platforms its IT runs
  on (GitHub, Microsoft 365, Azure, Teams, Planner, 1Password, an HR system, an expense system, Apple Business
  Manager, monitoring, backup and network vendors), no document attached;
- *never*: invoices, receipts, orders, licences, subscriptions, documents, requests, conversations, invitations;
  marketing (origin, or a marketing sender); any subject about a renewal, contract, licence, subscription,
  invoice, receipt, order or payment; a person's address at a vendor; replies and forwards; the helpdesk;
  voicemail; the accounting system and the licence reseller (except outage notices); scanners; colleagues'
  addresses;
- *protected*: Keep, in a binder, linked to a work item, a thread where a person wrote or the owner replied, flagged.

The selection was a few thousand messages from a few hundred senders and systems, about half alerts and half
internal, with the protected ones left out; most sat in a Notifications folder and the inbox.

| Step | Apply | Per message | Dry run | Failed |
|---:|---:|---:|---:|---:|
| 1 | 0.51 s | 511 ms | clean | 0 |
| 10 | 0.67 s | 67 ms | clean | 0 |
| 100 | 4.9 s | 49 ms | clean | 0 |
| 1,000 | 50.6 s | 51 ms | clean | 0 |
| 3,300 | 564 s | 171 ms (one chunk waited out a 6-minute throttle) | clean | 0 |

Afterwards only a few hundred of the incoming mail Talos calls alert or report were still in a folder; the rest
were in Deleted Items. What is left is protected or paperwork.
