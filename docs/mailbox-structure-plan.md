# Mailbox structure: from Talos metadata to the servers

Status: proposal, with the planner built. Nothing is written to a mailbox until the owner decides
the open points below, and then only through changesets they (or Claude on their instruction)
commit, scaled 1 → 10 → 100 → all as in the write-back test plan.

## What the metadata says (email only, accepted values)

The shape on one real archive, rounded:

| | Work account | Gmail |
|---|---:|---:|
| work / personal / undecided | ~78% / ~2% / ~20% | ~1% / ~74% / ~25% |
| short-lived / keep / undecided | ~75% / ~1% / ~24% | ~54% / ~8% / ~38% |
| largest topics | Work/Backup, Work/Monitoring, Work/Development, Work/Identity & access | Shopping, Music, School, Family, personal tech, Gaming |

Most mail is short-lived. The structure therefore has two jobs:
- keep the inbox for what needs the owner;
- make the few keepers (records, knowledge, memories) easy to find.

Everything else only needs to be out of the way, and still searchable in Talos.

The Gmail labels set by hand map onto topics (for example a marketplace site's label →
Marketplace), and a few are really types (Receipts, Notifications, Newsletters/*). The Talos
rules that copied labels and folders into topic/type were retired; Jev's topics replaced them.

## Proposal: organise by what the owner does with the mail, then by area

The same shape for both accounts. In Gmail it is labels; in Exchange, folders.

```
Inbox                      people writing to the owner, and asks (question, action, decision)
Keep/                      records and knowledge, kept for years
  Receipts & invoices      value record_financial  (types receipt, invoice, statement…)
  Contracts & legal        value record_legal
  Accounts & licences      value record_account
  Knowledge                value knowledge, decision, reference
  Memories                 value memory (Gmail)
Areas/                     where the rest lives, by topic family (one level, few folders)
  Gmail: Family & home, School, Money, Shopping & marketplace, Music, Leisure, Tech, Work-related
  Work:  IT Operations, Security, Cloud & Platforms, Costs & vendors, Customers & Business,
         People & Company, Learning & events
Automated/                 machine mail that is short-lived
  Alerts & reports         backup, monitoring, alarms (work account)
  Notifications            service and account notices
  Marketing & newsletters  offers, newsletters, cold sales
  Spam candidates          origin spam (never deleted by Talos; the owner decides)
```

**How each message gets its place**, deterministically, from its effective values:
1. **Inbox:** `sender_kind = people` with an ask, or anything newer than N days from a person.
   Otherwise it is archived out of the inbox.
2. **Keep/…:** `keep = keep`, by value.
3. **Automated/…:** `sender_kind = machine` and `keep = short_lived`, by type family.
4. **Areas/…:** everything else, by topic family.

The rules are plain Talos rules. When a mapping has matured, it runs on every sync and
produces a daily changeset.

**Undecided mail is not moved.** That is the mail (about a quarter) with no accepted values; it stays where it is
until a focused run or review decides it.

## How it reaches the servers

- **New namespace first.** The structure is built under `Talos/…` (Gmail labels; Exchange folders
  under a `Talos` folder). Old labels and folders are left alone until the owner has lived with the new
  structure. Moving mail out of the old folders is then one changeset per folder. Deleting an
  empty old folder is done by hand (Talos never deletes).
- **Gmail first.** Its write-back is tested and labels are cheap: one STORE per 1,000 messages.
- **The work account second.** This needs the Entra consent `Mail.ReadWrite` (a step the owner takes)
  and a dry run for Graph. Moves go 20 per batch request with throttling, so a full pass is a
  background job of an hour or more, checkpointed.
- **Exchange inbox rules.** Once Talos sorts incoming mail, the server rules either compete with
  it or duplicate it. The proposal is to remove them by hand (Talos has no permission for mailbox
  settings), keeping only rules that must act before Talos sees the mail, if any.
- **Incoming mail.** Every sync proposes moves for new mail as a changeset. At first it is
  reviewed (by the owner, or by Claude, which is cheap). Once a rule has been right for a while,
  its moves are applied automatically, within the size limit.
- **Rules from mature sorting.** When a sender or pattern has been decided the same way many
  times (20+, no overrides), Talos drafts a deterministic rule, switched off, for the owner to turn on.
  From then on, that pattern no longer needs Jev.

## Decisions (from an interview with the owner)

1. **Shape:** by what the owner does with the mail: Inbox / Keep / Areas / Automated, as proposed.
2. **Inbox:** people plus asks. Person mail older than 14 days with no open ask is filed.
3. **Namespace:** the new structure is built under `Talos/…` first, next to the old one. Mail is
   moved out of the old folders one changeset at a time; the owner deletes empty old folders by hand.
4. **Server rules:** the owner removes the Exchange inbox rules and Gmail filters by hand once Talos sorts
   incoming mail.
5. **Review:** at first the owner reviews every changeset themselves. Later the review is lifted to
   deterministic rules, to AI, or to both (Jev or a similar "system 1" model could be of use).
6. **Undecided mail** goes to `Talos/To sort`, so it is visible and can be worked through.
7. **Type gets two levels:**
   - a coarse *kind* that matches how the owner reads mail: conversation, question/request, FYI,
     announcement, invitation, offer, transaction, alert, report, spam;
   - the fine 64 types kept underneath as detail.

   This came from a blind test on the unsure cases, where the owner labelled type far more broadly
   than Claude, Haiku and Jev.
8. **The unsure cases** (tens of thousands): test again first. Once *kind* exists, a fresh blind 15 with the
   owner, Claude and Haiku. Whoever matches the owner best labels the bulk; Haiku if close, since it is cheaper.

### Blind test on unsure cases (set 2, 15 items)

Agreement with the owner on hard cases (items Jev was unsure of):

| Labeller | Agreement |
|---|---:|
| Claude | 57% |
| Haiku | 63% |
| Jev | 58% |

Type differed most, from granularity rather than mistakes: for example, a subscription renewal
or a resolved ticket is "FYI" to the owner and "subscription" or "ticket" to the models. On origin,
topic and route the models were about two thirds right.

## Built: the planner

Dry: it computes and proposes, and writes nothing to a mailbox.

- **Rules as data:** `rules/structure.json`, versioned. Per account an ordered list of targets, first
  match wins. The order, and why:
  1. *Left alone:* the owner's sent mail, drafts, and Exchange's own folders (sent, deleted, junk…).
  2. *Spam candidates*, unless the mail is also a record: a phishing mail posing as a person with a
     question must not keep its inbox place.
  3. *Inbox* (only mail that is in the inbox now; it is never refilled): flagged by the owner; a person
     with an open ask (an ask, active or proposed, and no reply from the owner in the thread since); a person, or
     undecided, under 14 days old.
  4. *Keep/…* by value: records outrank areas and automation, since they must be found for years.
  5. *Automated/…* for machine mail that is short-lived, by kind when there is one, else type, else
     origin. Before Areas, because most machine mail has a topic (Work/Backup, Work/Monitoring…) and
     Areas would otherwise absorb it.
  6. *Areas/…* by topic, the mapping written out per account (every taxonomy topic has one; a test
     holds that). The work account adds *Personal* for the personal mail in the work mailbox.
  7. *To sort:* nothing above decided it.
- **Plan:** `talos structure plan [--account A] [--dry-run]` stores one row per email message in
  `structure_plan` (migration 018). A few seconds per account on a large archive.
  `--dry-run` computes the same report in a read-only transaction. Teams messages are never placed.
- **Changesets (Gmail only):** `talos structure changesets --account gmail [--target T] [--limit N]
  [--new-since 1d]`: one `add_label` changeset per target, then one `archive` changeset for mail that
  leaves the inbox and already carries its Talos label on the server. All stay planned; the owner
  dry-runs and commits each with `--max`, in steps. Exchange: the plan and its counts only, until
  `Mail.ReadWrite`.
- **Incoming mail:** `talos sync --then-rules` places new mail in the plan after importance (never
  failing the sync) and looks again at inbox mail as it ages. No changeset is made on its own.
- **The Structure page** (Operations › Structure): the tree with counts, the inbox before and after,
  a place's rule and examples, "Why here?" for any example, the old places, and the prepared changesets.

## Kind: the level above type (built, decision 7)

`kind` is a dimension in `rules/taxonomy.json`, one value per message, with a definition written
from how the owner reads mail. Every type is listed under exactly one kind (`derived_from.values`; the
load refuses a type under two kinds, under none, or a key given twice), so the fine 64 types stay
as detail underneath.

| Kind | Types under it |
|---|---|
| conversation | conversation, introduction, reply_thanks, listing_message, document |
| question_request | question, request, inquiry, application, access_request, signature |
| invitation | invitation, calendar_update |
| fyi | fyi, calendar_response, meeting_notes, reminder, subscription, sign_in, password, account_change, welcome, policy_update, maintenance, service_update, license_asset, ticket, change_notice, dev_notification, social, listing_status, auto_reply, mail_notice, official_letter, notification |
| announcement | announcement, newsletter, event_webinar, digest, creator_post, survey |
| alert | alert, security_event, alarm, incident, security, usage_notice |
| report | backup, report |
| offer | promotion, sales_outreach, partner_offer, quote, job_ad, listing_alert |
| transaction | receipt, invoice, order, shipping, booking, payment, statement |
| spam | spam |
| other | other |

Against the owner's 15 answers on set 2 (their type read as its kind, and each labeller's type read
as its kind): Claude 9, Haiku 13, Jev 11 of 15, where on the fine type they had 4, 8 and 3. What no
mapping fixes: a social network's event invitation (owner: announcement; all three: type invitation),
and a software release note (owner: announcement; Claude and Jev: promotion).

**Derived and accepted like the boundaries.** A kind's probability is the sum of the probabilities
of the types under it, from the full stored scores (`talos.boundary`, KIND_MIN 0.85). On an answer
key (a few hundred items with a sure type; Jev's v2 context run) 0.85 decides 73% of the items at 94.6%
right (0.80: 78% at 93.5%; 0.90: 68% at 96.8%). `accept --two-level` accepts kind after the
boundaries and before the exact values; an exact type is then held back where the message's
accepted kind is another one. `unaccept` undoes it. Kind is a filter and a facet in Messages and a
card on the Acceptance page. Asked directly: `talos enrich jev focus --field kind`.

**Set 3** labels origin, kind, topic, ask, value and route (`gold_set.params.label_fields`). It is
drawn from the unsure cases as sender groups (five or more unsure cases of one sender; a shared
sender such as an IT relay address split by system: its [tag], else a trailing status word, else the first
word), single cases and Teams windows, never a message of an earlier set. A sender-group item
shows how many messages it stands for and a few of their subjects; "Mixed group" keeps the owner's
answer to the one message. `talos enrich gold propagate-groups --set 3` gives the owner's answers to the groups
(a dry run unless `--apply`).
