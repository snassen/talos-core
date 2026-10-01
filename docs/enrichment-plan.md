# Talos enrichment: a plan for crunching every message

Draft. Read-only research: no model was run and nothing was sent anywhere.
Sources: the Talos repo (`cases.py`, `importance.py`, `rules.py`, `events.py`, the SQL
migrations, `teams_ingest.py`, the seed rules), the live database (read-only SELECTs), and
an earlier Codex experiment with Jev (a 250-conversation decision benchmark, a spike with
local Splash models, and the `gmail-labeler` engine it calls).

## 0. What the data looks like

The figures below are the shape of one real archive (a work Microsoft 365 account, a personal
Gmail account and Teams, about a quarter of a million messages in all), rounded. Run
`talos enrich report` for your own.

| | Share of messages | Flagged `is_automated` (incoming) |
|---|---:|---:|
| Work mail (Microsoft 365) | about half | about a third |
| Gmail | about a quarter | a little under half |
| Teams chats | about a quarter | almost none |

- **`is_automated` misses much of the machine mail.** It fires on under 40% of incoming email,
  but tens of thousands of work messages that are *not* flagged produced a backup, alarm or
  alert event. Counting any machine signal (the flag, an event, or a rule type of notification,
  backup, alert or newsletter), about 70% of incoming email is machine mail.
- **The remaining 30% are mixed.** About a third come from people the owner has written to, in
  threads they replied in: clearly conversations. Another third come from senders the owner has
  never written to, in threads they never replied in. Half of those come from a little over a
  hundred senders with 20 or more messages each. These are almost certainly systems that send
  without automated headers. This is why "people and automated" does not separate them today.
- **Threads:** over 90% of email threads have one message, and well under 1% have 10 or more.
  Teams chats split roughly evenly into group and meeting chats, with fewer one-to-one; a couple
  of dozen very long chats hold a large share of the Teams messages. No channels are synced yet.
- **Senders:** of some ten thousand addresses that send incoming mail, under a hundred account for
  half of it, under a thousand for 80%. About half the addresses sent exactly one message. By
  domain: a handful of domains = 50%, a couple of hundred = 80%.
- **Existing metadata:** `type`, `topic` and `action` assignments, all from rules, and no model
  assignments or `model_run` rows yet. Almost all the seed rules match a Gmail label or an
  Outlook folder; only a few use a sender domain or a subject. So the rules mostly re-express
  how things were filed, not what they are.
- **Objects:** a few dozen objects (projects, areas, systems …) and work items, and almost no
  `member_of` or `about` edges. Routing therefore has almost no examples to learn from yet, so
  it must use each object's own description.
- **Incoming rate:** about 1,200–1,400 emails a month across both accounts (all directions),
  plus Teams. That is roughly 40–80 new items a day.

## 1. Jev: what it is, with evidence

- **Hosted, not local.** `gmail-labeler/lib/gmail_labeler/jev_client.rb` posts over HTTPS to
  `https://api.typesafe.ai/v1/systemone` with a Bearer TypeSafe API key (read from the
  `TYPESAFE_API_KEY` environment variable in the Codex runner). The model id is `jev-1.13.0`.
  The findings quote "the provider's published $0.042 per million input tokens". Every case
  sent therefore leaves the Mac: subject, sender names and addresses, and up to 1,800
  characters of body. By contrast, the Splash dense and MoE models ran on a local machine through an
  SSH loopback tunnel, so they stay on the owner's own hardware.
- **Interface.** One request per case: `{state: {fields…}, model, questions}`. A question is
  either a `choice`, with a closed set of values each carrying a one-line criterion, or a
  `noul`, a statement scored from 0 to 1. The answers come back with probabilities for every
  choice, a score for every statement, and token usage. Jev does not pick labels itself.
  Deterministic Ruby code routes on thresholds, for example spam if `likely_spam ≥ 0.70`, and
  a choice counts only if its probability is ≥ 0.70 and it leads the runner-up by ≥ 0.15.
  This atomic-question design is the "mechanism" worth keeping.
- **Speed.** 250 cases took 10–12.7 s with 8 requests in flight, about 20–25 cases per second.
  50 cases took 2.6 s. Packing 10 cases into one call was no faster and changed some answers,
  so keep one case per request and fan out.
- **Cost.** About 1,670 input tokens per case (7 questions, 1,800-character record). Metadata
  alone still cost 1,374 tokens, so about 1,300 tokens per call are fixed question overhead.
  Output (about 390 tokens) is free. The price works out to $0.018 per 250 cases and about $1
  per 14,000. It is cheaper to add questions to one call than to make a second call.
- **What the "decision benchmark" measured.** It ran a frozen sample of 250 conversations
  from an older Todo-later queue (not random) through Jev, Codex Luna and the Splash models,
  with four record shapes. It measured wall time, how many cases got a label, repeatability
  and cross-model agreement. **It did not measure accuracy: there was no gold set.**
- **Findings.**
  - The 1,800-character record resolved 202 of 250 cases, against 140 of 250 on metadata
    only.
  - Two identical runs agreed on 242 of 250.
  - Short and full records agreed on 219 of 250.
  - Cleaning the text gave no measurable gain.
- **Failure modes.**
  - It leaves cases unresolved when there is no body (49 of the 250 had none).
  - The routing code applies precedence that the owner disagreed with: receipt-over-topic,
    and a named company over a subject area.
  - It falls back to generic buckets. The Splash MoE model was far worse at this.
  - Spam is caught only because spam is asked as an explicit question. The direct-label
    models labelled 9 or 10 of the 10 spam holds.
- **Calibration.** The owner labelled 12 blinded boundary cases. Jev's routed label matched the
  owner's in 5 of the 12. Most misses came from routing precedence or labels not yet in its allowlist,
  not from its atomic answers. The lesson is to calibrate the *routing policy*, not only the
  model.
- **Reusable prior art.** `jev_priority_classifier.rb` already asks the questions this plan
  needs: `current_actionable`, `durable_significance`, `durable_reference`,
  `obsolete_or_bulk` and `malicious_spam`, each measured against an `evaluation_date`.

## 2. Goals, and what "good" means

| Goal | Good means |
|---|---|
| **Origin** (people vs automated, done properly) | Every incoming item says who produced it. See the value list below. The owner's person views contain only people. A "person via a system" (a ticket reply, a platform message, a shared-document comment) counts as a person. |
| **Message type** | One closed value per message. "Receipt" and "invoice" are never confused with "notification". |
| **Asks me / expects action** | For recent mail and Teams, every direct question, request, approval or commitment addressed to the owner is flagged. **Recall comes first:** missing one is worse than a false flag. Once they have answered, the flag clears by itself. |
| **Useful facts or knowledge** | Messages with durable reference value (contracts, decisions, credentials notices, specifications, prices, how-tos) are findable as such. Transient and bulk mail is not. |
| **Delete or archive candidates** | A candidate list the owner can trust enough to commit as a changeset after a glance at the counts, with a precision target of 99% for trash. |
| **Routing** | For each project or object, a short list of suggested threads and windows, with a reason for each. Wrong suggestions are rare enough that the owner keeps looking. |

## 3. Metadata schema, on the existing dimensions and assignments

All fields are `dimension` rows. Each value is an `assignment` on an entity, with
`source_kind`, `source_ref`, `status`, `confidence` and `evidence`. Model output lands as
`proposed`. A spam or unresolved hold stays a `hold:*` tag, as `cases.py` does today.

| Dimension | Entity | Card. | Closed values | Filled by |
|---|---|---|---|---|
| `origin` (new) | message; default on person/org | one | `person`, `person_via_system`, `transactional`, `notification`, `marketing`, `alert`, `list` | **Rules** for most: an event fired → `alert`; List-Unsubscribe plus a bulk mailer → `marketing`; a known correspondent in a thread the owner replied in → `person`; the name contains "via", or the Sender header differs from From → `person_via_system`. **Jev** for unknown senders without automated headers, per sender pattern. |
| `type` (existing, now closed) | message | one | `conversation`, `request`, `invitation`, `receipt`, `invoice`, `order`, `booking`, `newsletter`, `promotion`, `notification`, `alert`, `backup`, `security`, `document`, `other` | Rules already cover much of it. **Jev** for the rest, per pattern. |
| `topic` (existing, now closed) | thread / message | one | taxonomy v2 as approved | Rules cover some. **Jev** choice question, as in the Codex classifier. |
| `ask` (new) | message / Teams window | many | `question`, `action`, `decision`, `my_commitment` | **Rules** keep the existing `question`/`deadline` regex and the `answered` check from `importance.py`. **Jev** confirms and adds `action`, `decision` and `my_commitment`, for person-origin items from the last 90 days only. |
| `value` (new) | thread / pattern / window | one | `knowledge`, `record`, `transient`, `noise` | **Jev**: its `durable_reference`, `durable_significance` and `obsolete_or_bulk` scores map onto these. Rules add `noise` for alert and marketing patterns once accepted. |
| `route` (new) | thread / window | many | object ids of active objects | **Jev** choice question: the criteria are each object's name plus its "what belongs here" text, plus `none`. Rules add a candidate when a sender's domain or participants overlap an object's members. |
| `disposition` (new, derived) | message | one | `keep`, `archive`, `trash_candidate`, `unsubscribe_candidate` | **Rules only**, computed from origin, type, value, age, importance and object membership. It is never a model question. |

`evidence` holds the Jev probabilities, the question version, the pattern key, and "propagated
from case N" when a value was copied from a pattern's representative. `confidence` is the
chosen value's probability.

Two things to fix before this schema is used:

- **Store the automated reasons.** The parser records them (`automated_reasons`) but saves
  only the boolean.
- **Check the "Get rid of" rule.** The seed rule `action-get-rid-of` marks that Gmail label
  as `discard`, a trash candidate. In the Codex taxonomy, "Get rid of" means *selling or
  giving away possessions*, not "delete this mail". Check this before any trash changeset
  uses it.

## 4. The pipeline

1. **Deterministic pre-pass (SQL, seconds).** Build per-message features: the automated
   reasons, List headers, x-mailer, a Reply-To or Sender that differs from From, whether an
   event fired, the owner's replies, and the importance signals. Also build a **sender profile** per
   address: message count, how often the owner writes to them, subject-template count, and the share
   of mail with List-Unsubscribe or an event. The rules then fill `origin` and `type` where
   the signals are unambiguous.
2. **Collapse repetition.** Pattern key = sender address + subject skeleton (strip
   Re/Sv/Fwd, replace any token containing a digit, keep the first three words). Measured on
   the real archive:
   - Machine mail: about one template per four messages, and the templates with at least 3
     messages cover about three quarters of them. Jev sees **3 samples per template** and
     propagates only when all three agree (the cohesion check from the Codex spike), plus one
     sample per sender for the long tail. That is a **cut of about 88%** in cases.
   - Person-side mail: the unit is the **thread**, less the threads from bulk unknown senders,
     which are handled as patterns: about half as many cases as messages.
   - Teams: windows (section 5), about one per eight messages.
   - **Total: roughly 80% fewer Jev cases than incoming items.** Cost is not the main reason
     (see section 7). The collapse cuts review work by far more than 80%, because the owner
     reviews one row per pattern. It keeps answers consistent
     within a pattern, and it sends five times less text off the Mac.
3. **Jev batches through `cases`.** `cases.py` needs three extensions:
   - Several dimensions per run: one Jev call carries all of that case's questions, since the
     fixed overhead makes one call cheaper than several.
   - Case units other than a thread: a pattern, or a Teams window.
   - A small adapter, outside the core as the architecture says, that turns Jev's
     probabilities into decision lines (`case_id`, dimension, value, confidence, evidence).
   The routing thresholds live in Talos as versioned policy, not inside the adapter. The key
   comes from the Keychain, not the environment (promise 7). Requests fan out 8 at a time,
   with a checkpoint after every batch, as `JevRun` does.
4. **Validation, then proposals.** `import_decisions` already refuses a whole file on any
   unknown ID, duplicate or disallowed value. Add two more checks: holds are never
   overwritten, and every pattern decision is expanded to its members with a `propagated`
   note in the evidence. Everything lands as `proposed`.
5. **Review UI in Talos.** One queue, grouped by pattern and ordered by message count. Each
   row shows the proposed values as dashed chips, the reason, three sample subjects, and a
   count, with the actions *accept for N messages*, *change*, *reject* and *never ask
   again*. Separate tabs cover asks (recent people and Teams), routing (per object) and
   holds. Accepting records `decided_by`.
6. **Accept policy per field.** A per-field threshold replaces the single `min_confidence`.
   Nothing is auto-accepted until the gold set (section 7) shows the field meets its target:

   | Field | Auto-accept when | Otherwise |
   |---|---|---|
   | origin, type, topic | Jev ≥ 0.85 with a margin ≥ 0.30, agrees with the rule prior or with a second pass, and the gold precision target is met | review queue |
   | value = noise | on machine-origin patterns only, same conditions | review |
   | value = knowledge/record, ask, route | never | shown as proposals in context: "check this", the object page, the waiting list |
7. **Rules grow out of repeated decisions.** When a sender or pattern has at least 20
   accepted decisions for a field, all identical, and no human overrides, Talos drafts a rule
   (`rule:auto-…`, **disabled**) with its preview count. The owner switches it on. Jev then only sees
   new patterns. Track "share of incoming mail decided by rules" as the health metric; it
   should rise every month.
8. **Changesets for bulk actions.** A cleanup is a saved selection over effective values,
   for example: origin in (marketing, notification), value = noise, older than 12 months, no
   human mark, not in any object. It goes through the existing plan → commit → apply →
   undo. The first targets are *archive* and Talos/… labels, not trash. Restructuring into
   a better base layout is the same mechanism: `move` or `add_label` per value of `topic`.

## 5. Teams is different: the unit is a conversation window

A Teams message is short (74 characters on average, median 43). One line out of context says
almost nothing. A **window** is a run of messages in one chat with no gap longer than 2 hours.
Measured: on average about 8 messages a window and a median of 4, so most fit in 1,800
characters. Cap a window at about 30 messages or 2,500 characters and split longer ones.
Include the last three messages of the previous window as context.

Extract per window:

- **Asks of the owner:** a question or request after their last message, @mentions, and
  one-to-one chats first. Deterministic `answered` logic clears them.
- **The owner's own commitments** ("I'll fix it", "jag kollar"). Teams holds tens of thousands
  of the owner's own messages, and this is where promises hide.
- **Decisions made**, and **facts** (links, shared files, numbers, names of systems).
- `topic`, `route` and `value` for the window.
- Skip `origin`: almost no Teams messages are automated. Meeting chats should later be
  joined to calendar context.

Store window results as assignments on the chat thread, with the window's first and last
message ids in `evidence`, or add a light `window` object if the UI needs to link to them.

## 6. Backfill, then incremental

**Backfill, in stages, each gated on the previous one:**

1. **A. Deterministic only.** Pre-pass, sender profiles, templates, rule priors for `origin`.
   Report coverage.
2. **B. Gold set.** Label about 300 items, then run Jev on them only. This costs pennies and
   sets the thresholds.
3. **C. Machine patterns.** The template cases for origin, type, topic and value. Reviewed per
   pattern.
4. **D. Person threads.** The last 12 months first, then older ones. `ask` only for the last
   90 days; older threads get topic, value and route
   only.
5. **E. Teams windows.**
6. **F. Rule drafting**, then the first cleanup changesets as *plans only*, for the owner to commit.

**Incremental, on each 5-minute sync:** ingest → rules → events → importance (all of which
exist today). Then match patterns: an item whose pattern already has accepted values gets
them by rule or propagation. New patterns, and person mail that needs `ask`, are queued. Send
the queue to Jev at most every 15 minutes. Expect 10–30 cases a day: seconds of work and well
under a cent. Person mail with an `ask` goes first, so "waiting for you" is right the same
hour.

## 7. Evaluation

- **Gold set:** about 300 items, stratified: 100 machine patterns, 100 person threads, 50 Teams
  windows and 50 known boundary cases (receipt vs topic, platform vs authority, zero-cost
  purchases, spam). The owner labels them **blind** (no model answer shown) in the Talos review
  screen, six at a time as in the Codex calibration. That is about 5 sessions of 10–15 minutes.
- **Calibration:** bucket Jev's probabilities (0.5–0.7, 0.7–0.85, 0.85–0.95, >0.95) against
  the gold labels and set each field's threshold where precision meets its target. Rerun on a
  fresh sample after any change to a question or routing policy. Keep repeat-run agreement
  as a stability check: it was 97% in Codex.
- **Precision targets before any auto-accept or write-back:**

| Field | Precision | Recall | Note |
|---|---:|---:|---|
| origin | 97% | — | person vs machine errors are what the owner sees most |
| type, topic | 95% | — | |
| value = noise | 98% | — | feeds cleanup |
| disposition = trash_candidate | 99% | — | and still a committed changeset |
| ask | 80% | 90% | a missed question costs more than a false flag |
| route | 80% top-1 | — | always reviewed |

## 8. Throughput and cost

Assumptions:

- About 2,200 input tokens per case. That is the measured 1,670, plus the route criteria and
  the extra questions.
- 20 cases per second at 8 requests in flight.
- $0.042 per million input tokens; output is free.

| Run | Cases | Input tokens | Cost | Jev time |
|---|---:|---:|---:|---:|
| Gold set | 300 | 0.7 M | < $0.03 | 15 s |
| Full collapsed backfill, one pass (for ~230,000 incoming items) | ~48,000 | ~106 M | **~$4.5** | ~40 min (1–2 h with rate limits) |
| The same with two passes for consensus | ~96,000 | ~210 M | ~$9 | ~1.5–3 h |
| For comparison: every incoming item, uncollapsed | ~230,000 | ~510 M | ~$21 | ~3.5 h |
| Incremental | 10–30 a day | < 0.1 M | < $0.01 a day | seconds |

Money is not the constraint. The constraints are review effort, the privacy of what is sent
(section 10), and the serial write-back speed if a changeset touches tens of thousands of
messages. The Codex Apple Mail writer managed about one conversation per 3 s. Talos's
IMAP and Graph batch executors should be far faster, but measure them before planning a large
cleanup.

## 9. One tag system for cards, objects and messages

Everything is already an `entity`: messages, threads, objects, work items and notes (since
`009_work.sql`). So one assignment layer can serve as:

- **Message metadata:** origin, type, topic, ask, value.
- **Card labels on the work board:** assignments on `work_item` entities in the same
  dimensions (`topic`, `area`, `tag`, plus perhaps `priority`).
- **Object tags:** assignments on `object` entities.

Consequences:

- One filter such as `topic = Insurance` returns messages, cards and objects together.
- When a card is created from a message (`about` edge), the message's effective topic and
  route are copied to the card as **proposed** values.
- Rules, models and the owner's own hand all write the same table with the same precedence (human >
  rule > accepted model > import).

**Colours:** add a `color` to `dimension`, one hue per dimension: for example topic blue,
type grey, ask orange, value green, route purple, area teal, tag neutral. Allow an optional
per-value override in `allowed`, which becomes a list of `{value, color}` objects. Every
entity shows the same chip, so a colour always means the same dimension anywhere in Talos.
The source shows in the chip's style: solid for active (human or rule), dashed outline for a
model proposal, a small marker for "via thread", and rejected values hidden. Account colours
(F04) stay separate, as a left stripe rather than a chip.

## 10. Privacy and safety

- **What leaves the Mac:** only the Jev case records sent to TypeSafe's hosted endpoint. A
  record holds a subject, the senders, a text excerpt of up to 1,800 characters, and the
  question texts. Nothing else is sent: no vault originals, attachments, database rows,
  embeddings or bulk exports.
- **Minimise what is sent:**
  - Send one representative per pattern.
  - Replace email addresses with a role and domain (for example "person at client domain").
  - Mask Swedish personal ID numbers, card and IBAN numbers, phone numbers, and URL paths
    (keep the host).
  - Never send attachments.
  - Keep a **never-send list**, enforced before export: health, HR and employment, legal,
    security incidents, and any domain or object the owner marks as confidential.
  - Items on that list stay deterministic, or go to a local model, which was slower and
    less reliable in Codex but stays on the owner's own hardware.
- **Company data:** work mail and Teams are company data. Whether hosted Jev may process them is
  a decision for whoever is responsible for that data's security; here it was allowed.
- **The key** lives in the Keychain. Each `model_run` records the exact file hash, so what
  was sent is auditable.
- **Nothing is auto-applied to mailboxes.** Model output is `proposed`. Write-back stays off
  per account until the owner enables it. Every server change is a changeset the owner commits. Trash is
  the furthest any change goes, and nothing is permanently deleted. Spam holds are never
  overridden by a model. The guard tests stay as they are.

## 11. Decisions (from an interview with the owner)

1. **Hosted Jev may see everything:** Gmail, work mail and Teams. Masking ID, card and phone numbers stays as good practice.
2. **Fields: all of them.** origin, type, topic, ask (including the owner's own commitments), value
   and route, plus the rule-derived disposition.
3. **Unit of work: to be found in testing.** The best way to use Jev is not known yet, so the
   first test compares a per-pattern/thread/window unit against per-message on the answer key
   (accuracy, cost, time), and the better one goes forward.
4. **Answer key: about 300 items**, labelled blind in about five short sessions in Talos.
5. **Auto-accept: any field**, once it is above a target that is set *after* testing Jev's
   capability and quality on the answer key. It applies inside Talos only; mailboxes change
   only through changesets the owner commits.
6. **Asks: 90 days to begin with.** Go further back if it proves quick and cheap. The asks
   data may later be used for other insight, for example who asks what, and how often.
7. **One pass first.** Then a quality test: Claude spot-checks a random sample, and the owner
   spot-checks another. A second pass for consensus only if the test shows it is needed.
8. **Rule drafting:** after 20 identical accepted decisions with no override, Talos drafts a
   rule that starts **switched off**. The owner enables it.
9. **First cleanup: decided after the owner has seen the metadata and the counts in Talos.** Nothing
   is planned yet.
10. **"Get rid of"** is a legacy label left from an old cleanup project. It is not important
    mail, but not to be deleted either: some of it is worth keeping. The seed rule now gives the plain tag `legacy:get-rid-of`
    (rule version 2). Its old `action = discard` values are gone.
11. **Asks are ordinary metadata,** usable anywhere: a flag on the message, a filter in
    Messages, a condition in rules, and the Needs attention widget.
12. **Tag colours: one colour per tag family** (topic, type, ask, value, route…), with a
    legend. Model proposals get a dashed outline, accepted values are solid.

### Next steps

- **A. Rules-only pass.** Sender profiles, subject patterns, origin and type from existing
  signals, and a coverage report. Needs no key.
  *Built:* `talos enrich prepass` and `talos enrich report` (`talos.enrich`,
  migration `010_enrich_prepass.sql`). The automated reasons turned out to be stored already,
  in `message.headers -> 'automated'`, since P0; reparse now refreshes them. `type` is a
  closed list (the values above). The origin signals and their thresholds are listed in the
  module's docstring; a message where a person signal and a machine signal disagree stays
  undecided for Jev.
- **B. Answer-key screen in Talos.** Blind labelling six at a time. Needs no key.
  *Built:* `talos enrich gold sample|report|list` and Operations › Answer key
  (`talos.gold`, migration `011_gold.sql`). A set is frozen at sampling (a seed makes it
  reproducible; sampling again makes a new set). Strata for 300: 50 boundary (15 origin by
  `bulk_mailer`, 8 by `correspondent`, 7 receipts with a topic, 7 person via a platform, 6
  legacy:get-rid-of, 7 bulk unknown senders), 100 machine patterns (40 big templates drawn by
  size, 30 tail, 30 origin undecided), 100 person threads (35 last 90 days, 30 last year, 35
  older), 50 Teams windows (17 one-to-one, 17 group, 16 meeting). Each item is anchored on one
  message with its unit (pattern samples, thread, or a 2-hour window by first and last id).
  Labels live in `gold_label`, apart from assignments. The item API is blind (no assignment,
  importance, automated flag, mailbox label, stratum or reason); the agreement preview comes
  only after a finished round, and an answer changed after it is marked. `gold.compare()` scores
  any predictor per field, value, source and confidence bucket; `gold.predictions_from_run()`
  feeds it a model run (step C). People / Automated in Messages now go by origin (machine
  values are automated; no origin falls back to the automated flag), and Origin is a facet.
- **Taxonomy v1** *(built)*: `rules/taxonomy.json` holds the closed lists of origin,
  type, topic, ask, value and route, each value with a family, a label and a one-line
  description; it supersedes the value lists in §3's table. `talos taxonomy load` (also run by
  `talos setup`) writes them to `dimension.allowed` and `dimension.value_meta` (migration
  `012_taxonomy.sql`), and refuses while a dropped value is still in use. Route is now a work
  category by name, not an object id. `list` is a people origin; the machine origins add
  `system_report`, `mail_system`, `auto_reply` and `spam`. Step A's signals follow (event,
  type and bounce at version 2, a new quarantine and auto_reply signal); the answer key and the
  Messages dropdowns show the labels, grouped by family.
- **C. Jev adapter in `cases`.** It reads its key from the Keychain as `typesafe-api-key`, which
  the owner adds themselves. It runs first on the answer key only, comparing units (decision 3).
  *Built:* `talos enrich jev gold --set 1 --unit message|context
  [--dry-run]` and `talos enrich jev report --run RUN [--compare RUN2]` (`talos.jev`, `talos.mask`,
  `talos.policy`, `talos.jev_gold`, migration `014_jev.sql`). Evaluation only: no assignments.
  One-value fields are one `choice` each; ask and route are one `noul` statement per value.
  A Teams item is its whole window in both unit designs, so the designs differ only on email and
  the report scores email and Teams apart. The questions cost about 6,600 input tokens per case
  (type and topic are two thirds of it), so a case is about 7,000: 300 cases ≈ 2.1 M tokens ≈ $0.09.
- **Backfill (stages C–E)** *(built)*: `talos enrich jev backfill --stage
  machine|person|teams [--since DAYS] [--limit N] [--max-cases 60000] [--budget USD] [--run RUN]
  [--dry-run]`, `talos enrich jev backfill-report --run RUN`, `talos enrich accept|unaccept --run RUN`
  (`talos.backfill`, migration `015_enrich_backfill.sql`). Units as in §4.2 and §5, one case per
  template sample (3 per template of 3+), per small template (sender + template, not per sender),
  per person thread (anchored on its latest incoming message) and per Teams window; records are
  the answer key's (`gold.unit()` → `jev.record()`, context design, templates v2). Answers land as
  proposed `assignment` rows (source_ref = the run); a template's other messages get a field only
  when its three samples agree. Accept is per field (default origin, topic, value 0.85, type 0.90,
  margin 0.15); an accepted model value still ranks below rule and pre-pass values.
  Sized on a real archive of about a quarter of a million messages: some 80,000 cases; about
  480 M input tokens, about $20, about 50 minutes at 26 cases/s.
- **Two-level acceptance** *(built)*: not every mistake costs the
  same. Four boundaries, new dimensions derived by family (`derived_from` in rules/taxonomy.json,
  `talos.boundary`, migration `016_two_level_accept.sql`): `sender_kind` people | machine (from origin:
  person, person_via_system and list are people), `sphere` work | personal (from topic: Work/),
  `form` conversation | other (from type: the Conversation family), `keep` keep | short_lived (from
  value: Keep and find, Records). A side's probability is the sum of Jev's full stored scores, decided
  at 0.90; a Teams window's sender_kind is people, fixed. Exact values (origin, type, topic, value) at
  0.70 with a margin of 0.15, and only on the side of the message's accepted boundary, or where that is
  undecided. `talos enrich accept --run all --two-level [--dry-run]` (the per-field preset stays the
  default); `unaccept` undoes it. People / Automated reads sender_kind, then origin, then the flag
  (human > rule/pre-pass > model); Messages has Work/Personal and Keep filters, and the message pane
  shows values as tags, dashed when proposed, solid when accepted.
  *Acceptance explorer* (Operations › Acceptance, `talos.acceptance`): per field a threshold slider
  (0.50–0.95) with, live, the answer key's decided and right shares, the costly errors, the archive's
  count (histograms in buckets of 0.05), the top confusions and five messages just over and under the
  line; Apply previews (a dry run) and then applies.
  *Focused runs* (`talos.focus`): `talos enrich jev focus --field F --where uncertain|disagree|all
  [--gold] [--dry-run]` asks one question per case (sender_kind as a binary choice), about 900–2,500
  tokens a case against about 6,100 for the full set; `--gold` asks answer key 1 first and an archive
  run is refused until it has. A newer run supersedes an older one's proposals for the same message
  and field. `--field a,b,…` asks several fields in one combined run: each unit once, with
  only the fields it is unsure of (on a large archive, about 40% of the separate cases); `--field a,b,… --gold`
  checks them on the answer key together.
  *Kind from type*: on the answer key, kind derived from the full set's type scores beat kind asked
  directly (more decided, and about 95% right against 89% at ≥ 0.85), but the newer direct runs had superseded
  it. accept now brings back a message's newest derived kind where no kind counts and it clears the kind level
  (`decided_by 'kind-from-type:…'`; unaccept and newer runs treat it as a policy row). New mail is also asked
  sender_kind directly (talos.incremental FOCUS_FIELDS), as the combined run asked the archive.
- **Incremental** *(built; §6)*: `talos enrich jev new [--backlog] [--since 7]
  [--max-cases 500] [--budget USD] [--dry-run] [--status]` (`talos.incremental`, migration
  `022_incremental.sql`). New = incoming e-mail and Teams lines with no model proposal, no answer-key group
  value and in no case (the owner's own mail never); by default received in the last 7 days, `--backlog` all.
  Units as the backfill's. Reuse without Jev: a template whose three samples agree on every field (the
  focused kind and value answers counting), a thread or window that grew by fewer than 3 messages since
  its case, and an applied answer-key sender group (the owner's values, as propagate-groups gives them). The
  rest is asked the full v2 set, then the focused kind and value questions as the focus runs asked them
  (three runs, purpose `enrich-incremental`; the runs chain holds). Accepted by the policy last applied
  (or `accept` in `TALOS_HOME/enrich.json`), boundaries and kind first; the touched mail is placed again.
  `talos sync --then-rules` runs it when `enrich.json` says `{"enabled": true}` (default off), at most
  every 15 minutes, $0.50 a day, 500 cases a run; it never fails the sync and checks in with Argus as
  `talos-enrich`. On a real archive the first run found a few hundred unjudged incoming messages,
  mostly in the smaller accounts; a few were reused, and the rest cost about $0.10.
- **Then:** the thresholds (decision 5), one backfill pass (decision 7), spot checks, and the
  review queue.
