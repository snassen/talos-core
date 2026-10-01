# The Talos manual

The live manual: the parts of the Talos field guide (a longer PDF, kept outside the repository) that describe a
place or a control, rewritten for the pages as they are now. Talos Web shows each section when you press the
small ⓘ beside the thing it describes (`talos.manual`, `/api/manual`); nothing else reads this file.

How it is written, so the parser (src/talos/manual.py) can read it:

- A section is `## Title {#id}`. The id is what app.js passes to `helpBtn('id')`; tests/test_manual.py fails
  when app.js names an id that is not here, or a section here is never used.
- Under the heading: an optional `Guide: …` line (where the field guide tells the longer story), then
  paragraphs, `### subheadings`, `- ` lists, `1. ` numbered steps and `> ` tips. Bullets may run over several
  lines when the next line is indented.
- Inline: `**bold**`, `*italic*`, and backticks for a button or a key as it reads on screen.
- An optional last line `See also: id, id` links to other sections.

No real mail, names or counts here: the repo is not the place for them, and counts go stale. When the screen
and the manual disagree, the screen wins; fix the manual in the same commit that changes the screen.

## Mail {#mail}
Guide: chapter 9, Messages

Every mail from every account, filtered by anything Talos knows. You will not read them all; the filters
are there so you don't have to. Teams has a place of its own (see Teams).

- **Search** understands Swedish and English word forms, and email addresses. `⌘K` jumps to it.
- `Messages` / `Conversations`: one row per message, or one per thread.
- The **filter chips** (Direction, Sender, Attachments, Work or personal, Keep, Sureness) are the boundaries as buttons.
  A set filter is a filled chip that says what it keeps; its × clears it.
- `Watch` saves the selection as a watcher, `Aggregate` counts it in groups on Discover, and `Reset` goes back
  to received mail in every account.
- `↑` `↓` or `j` `k` walk the list; with the pane open, it follows along.
- A **red chip** says a mail has left the mailbox: *Purged* (in Deleted Items, moved there by a Talos cleanup),
  *Deleted* (in Deleted Items, moved there outside Talos) or *Not on server*. The server's folders decide, so a
  mail restored from Deleted Items loses its chip. The pane says which cleanup moved it, and when.
- The **Sureness** chip asks how sure the values you filter on must be (see How sure).

> **Pro move.** Solo your work account, choose Sender: People, and hide the colleagues who copy everyone. What's
> left is the mail that is actually for you.

See also: accounts, senders, values, watchers

## Teams {#teams}

Every chat and every channel thread from Teams, in a place of its own, apart from mail.

- **The kind** above the list: Everything, One-to-one, Group chats, Meetings or Channels. Each row says its kind:
  1:1, Group, Meeting, or Team › Channel for a channel thread.
- **Direction** is both ways unless you narrow it: a chat is both sides of it.
- The **sidebar**: the chats by kind, then your teams, each folding open to its channels. A click narrows the
  list to one; ✎ beside a channel starts a new post in it. Then the people, and the filter panels (kind, origin,
  type, topic), as in Mail.
- `Conversations` / `Messages`: a found **message** opens its conversation right where it was said, marked, with
  the messages before and after it (Load earlier, Load later).
- **Writing**: the box under a conversation writes in that chat, or replies in that channel thread. **Enter**
  posts it at once (Shift + Enter for a new line); the box belongs to that one conversation, so there is no
  separate confirmation. A new post in a channel (✎) is longer: there ⌘/Ctrl + Enter or `Send` posts it. Plain
  text; no @mentions, edits or deletions from Talos. At most 120 posts an hour, as a safety net.
- **Live**: Talos looks for new chat messages every 20 seconds, all the time (the header says when it last did),
  and the open page shows what arrives within about ten seconds of that. Channels come with the 5-minute sync.
- **Boost after you post**: for two minutes after a post, Talos looks every 5 seconds where you posted (the chats,
  or that channel), on any page, and the conversation shows the answer as it comes (the header says "looking
  every 5 s"). Each post starts the two minutes again; then it goes back to every 20 seconds by itself. What you
  are writing stays in the box while the conversation updates round it.

## The account chips {#accounts}
Guide: chapter 4, The archive

One chip per account, in the account's colour, which is also its colour on every row. It works like a
mixing desk:

- Click a chip to switch that account in or out.
- Click its `S` to **solo** it: only that account is shown. Click the lit `S` again to get back exactly the
  accounts you had before. Option- or ⌘-click on a chip does the same.

### How mail gets in
1. **The five-minute walk**, always on, for every account including Teams.
2. `Sync mail now` fetches the mail accounts at once and says how many new messages came in. Teams keeps its
   own, slower rhythm.
3. **Imported files** (`talos import` on the command line), for old archives.

Reading never changes a mailbox: every account is opened read-only.

> **Mail to yourself.** Mail from one of your addresses to another counts as both received and sent, so the
> Received filter shows it too.

See also: sources, mail

## The sender sidebar {#senders}
Guide: chapter 9, Messages

Who sends the mail you're looking at, most first. The bars are blue for people and grey for automated mail.
`Domains` groups them by organisation.

- Click a sender to narrow the list to them; click again to widen.
- The eye **hides** a sender from the list *and every count*; hidden ones become chips you can undo.
- `Show all` lists every sender, and the header folds it again.
- Under the senders, one panel per filter with a long list: kind, origin, type, topic and label.
- × closes the sidebar; the `› Senders & filters` tab brings it back.

See also: mail

## Values: what a mail is {#values}
Guide: chapter 5, Values

Every message carries **values** in a set of fields. Four of them are **boundaries**, coarse splits where a
mistake costs something real: person or machine, work or personal, the form, and whether it is worth
keeping. The rest are **exact fields** (kind, type, topic…) that say more once the boundaries are settled.

### Who gets to decide
1. **You.** Your answer-key labels and your Important / Not important marks. You always win.
2. **Rules.** Conditions like "a site DOWN is an alert". Certain, cheap and explainable.
3. **Jev**, the hosted model, which answers every field with a confidence. New mail is judged every quarter of
   an hour, within a daily budget. Values Claude gives a sender group sit at this level too.

Jev's answer only **counts** when it is sure enough; Acceptance sets how sure. Below that line it stays a
suggestion, drawn as a **dashed** pill. Values that count are **solid**; point at one to see who set it.

See also: acceptance, rules, answer-key, sureness

## Importance {#importance}
Guide: chapter 9, Messages

Each mail gets an importance score from signals you can see, with their points:

- a person wrote it (+20), and sent it to you directly (+20);
- it's from someone you've written to (+15);
- it asks you something that is still unanswered (+20);
- a machine sent it (−30).

`Important` and `Not important` overrule the score for that mail. **Most important today** is the last
24 hours by this score, one row per conversation.

See also: waiting, values

## Waiting for your answer {#waiting}
Guide: chapter 11, The Overview

Questions to you from people you know, still unanswered, **oldest first** and one per conversation. The
number on the right is how many days they have waited.

Answer the longest-waiting person first. If it needs more than two minutes, open it and `Make a work item`:
the mail stays linked to it.

See also: importance, work

## Needs you {#needs-you}
Guide: chapter 7, The Work space

The short list of what's burning: work that is overdue, blocked, due for review or marked focus, and watchers
with news. Click a line to open it.

> **If this list is long,** that's information, not failure. Move things honestly: *Someday* is a perfectly
> respectable column.

See also: work, watchers

## Writing mail {#compose}
Guide: chapter 9, Messages

Pick the account in the **From** box; your default is marked. The box is big and in the account's colour on
purpose: nobody should send from the wrong account by accident.

- Warnings, such as a missing subject, show under the text.
- A mail goes only after `Send` **and** a second confirmation that names the account. The confirmation is for
  that exact mail, and any change asks again.
- The draft is saved in Talos while you type; nothing reaches a server before you confirm.
- `Drafts` lists what you started. Signatures (per account, for new mail and replies) and your default
  account are kept there too.

No rule, job, model or Claude can send. Only you.

See also: mail

## Work items and the Board {#work}
Guide: chapter 8, Work items and the Board

Mail is what others want from you. A **work item** is what you decided to do about it. It lives in one of six
columns, has at most one **home** binder (a solid pill) and any number of **related** ones (dashed), and may have
a due date, a review-after date and a focus flag. It can link to the mail it's about, and every change is
logged.

- Drag a card to another column, or focus it and press `m`.
- The chips on top: `Inbox`, `Focus`, `Overdue`, a search, a status and a binder. `Board`, `List` and
  `Timeline` are the same work, three ways.
- Click a card to open it in the pane: title, status, binders, dates, text, linked mail and history.
- `✓ Done` beside the status marks it done and saves in one click (with any other change you made in the pane).

### Four ways to make one
1. `New work item` here, on Today or on a binder's page (already homed there).
2. `Make a work item` while reading a mail: the mail stays linked.
3. `Link to a work item…` adds more mail to one that exists.
4. The vault import brought your earlier boards along.

See also: needs-you, binders

## Binders and their kinds {#binders}
Guide: chapter 6, Binders

Mail arrives by sender and date, but your work is organised by *things*: a firewall, a renewal, a customer, a
field you answer for. A **binder** is where one such thing lives: its description, its mail, its notes and its
work items. Talos sometimes says "object"; it is the same thing.

- **Area**: a responsibility you keep. It never ends; it is judged by staying in good shape.
- **Project**: an effort with a goal. When it's reached, the project is done and archived.
- **System**: a thing you run. Its page gathers its alerts and conversations. It ends when retired.
- **Topic**: a subject you follow across areas, a shelf of knowledge rather than a job.
- **Personal project**: a project outside your work.
- **Case, collection, saved search**: a single matter, a hand-picked set, a binder that *is* a live search.

> **The one-question test.** "When is it finished?" If there's an answer, it's a project. If the answer is
> "never", ask whether you *run* it (a system) or *answer for the whole field* (an area). If you only want to
> *know about* it, it's a topic.

See also: binder-page, areas, binder-members

## A binder's page {#binder-page}
Guide: chapter 6, Binders

From top to bottom:

1. **The header**: `Direct` / `Include nested` (whether the members of the binders inside it count), `Rename`,
   `Describe`, `Archive`.
2. **About**: the purpose and text in tidy sections, and a facts panel with open work, linked mail, notes,
   lifecycle and state. `Edit` changes the text.
3. **Board**: this binder's work items. `New work item` makes one already homed here.
4. **Found in your mail**, **Neighbours** and **Watchers**.
5. **Activity**: work changes, linked mail and notes, newest first.
6. **Notes**, **Members** (everything in it, with where it came from) and **In common** (the people,
   organisations and labels its mail shares).

See also: found-in-mail, neighbours, binder-members, watchers

## Ways into a binder {#binder-members}
Guide: chapters 6 and 16

A binder's members come in five ways, by hand, in one click or by themselves:

- `Add` in **Found in your mail**, building it up thread by thread;
- `Add to object…` on a mail you're reading, when you know where it belongs;
- `Take in` on a watcher, keeping it filled in one click;
- a **rule** pointing at the binder, for a clear and lasting pattern;
- a **stored query**, when the binder *is* a search and new mail should join by itself.

An exclusion always wins: `Not this` keeps a thread out for good, and `Let back in` undoes it.

See also: found-in-mail, watchers, rules

## Found in your mail {#found-in-mail}
Guide: chapter 6, Binders

The binder goes fishing. Every binder has **search terms**: its name, unless you give it better ones (a
product name rather than the project's). Talos searches the whole archive for them, as phrases, and lists the
threads that aren't in the binder yet. Threads *you took part in* come first, then threads from people, then
the rest.

- `Add` puts the thread in the binder; `Not this` keeps it out for good.
- `Change` edits the search terms: product, vendor and domain names work best.
- `All in Messages` opens the full search.

Ten minutes of `Add` and `Not this` gives a new binder its history.

See also: neighbours, watchers, binder-members

## Neighbours {#neighbours}
Guide: chapter 6, Binders

Binders linked to this one, and binders whose search terms turn up in the **same threads**. The count says
how many threads they share. It shows at a glance how your systems and projects touch each other; link the
obvious ones.

See also: found-in-mail, binder-page

## Areas {#areas}
Guide: chapter 7, The Work space

Your responsibilities from above. Each area is a card, with its projects, topics and systems inside as
coloured pills. Start here on Fridays, or whenever you've lost the thread.

### Reading an area card
- The top line counts what's inside.
- The pills are the binders: click one to open it.
- The bottom line is the signal: **overdue** and **blocked** first, then open work items, then how many mails
  this month mention its binders.

A binder is "in" an area when it is part of it or linked to it. **Still to place** holds the binders with no
area yet: open one and add it to an area under "Part of", and it moves onto the map.

See also: binders, watchers, needs-you

## Watchers {#watchers}
Guide: chapter 7, The Work space

A **watcher** is a saved search that keeps looking. It counts everything that matches and tells you what
arrived since you last looked. A watcher can belong to a binder; then `Take in` adds its new mail to that
binder in one click, and only when you say so. Pause one when it gets boring.

Three ways to make one:
- `Watch` in Mail keeps exactly what you filtered;
- `Add a watcher` on a binder watches its search terms and fills it;
- `New watcher` on Areas is a quick search by words. On Discover, `Watch` turns a renewal or a silent system
  into a watcher.

See also: mail, found-in-mail, needs-you

## Discover {#discover}
Guide: chapter 10, Discover

Discover goes through the archive looking for patterns. It needs no model: it just counts carefully. It
refreshes by itself when new mail arrives or a new day starts; `Look again` forces it.

- **Forgotten**: people who asked you something recently and are still waiting, oldest first. `Open` and
  answer, or make a work item.
- **Recurring**: renewals, licences and expiries from the same sender in the same month, two years or more,
  for the months ahead. `Watch` it, or make a work item due a month earlier.
- **Cross-connection**: organisations you write to often that no binder mentions. `Make a binder` comes
  filled in with the name and domain.
- **Gone quiet**: automated senders that wrote regularly, then stopped. Did a backup die quietly?
- **New**: people who wrote for the first time lately, and whom you've answered. File them while you still
  remember who they are.

See also: aggregations, watchers, binders

## Saved aggregations {#aggregations}
Guide: chapter 10, Discover

An aggregation is a Mail selection counted in groups (by sender, domain, year, month or account) and kept up
to date. Click a row to open its bars; click a bar to see those mails. `Aggregate` in Mail makes one; Claude
can make more.

See also: discover, mail

## Insights {#insights}
Guide: chapter 11, The Overview

A page of widgets you choose with `Customize`. The defaults cover what needs you; the statistics are there
when you're curious.

- **Argus**: are the Mac's services up, and is the heartbeat beating?
- **Needs attention**: work in progress, blocked, overdue, due for review, in focus or still in the inbox.
- **Waiting for your answer** and **Today's most important**.
- **Check this**: stalled important threads, and contacts left waiting.
- **Projects**, **Top people**, **Top domains**: click one to list its mail.
- **Events**: machine mail by status.
- **Low-hanging fruit** and **Improvement jobs**: the cheapest ways to make Talos smarter.
- **Archive**, **Accounts**, **Labels**, **Attachments**: statistics, off by default. Accounts has a
  `Sync mail now` button.

See also: waiting, importance, events, fruit

## Events {#events}
Guide: chapter 13, Keeping watch

Alarms, alerts and backup reports are read as **events** with a status: **failed**, **warning**, **ok** or
**info**, newest first. "Did anything break last night?" becomes one glance instead of forty alert mails.
Filter by status or by kind.

See also: argus, insights

## The answer key {#answer-key}
Guide: chapter 5, Values

The yardstick for rules and for Jev: a sample of mail you label **blind**. Talos hides its own answers until a
round of six is done, so your labels stay honest.

- One field at a time. Number keys `1`–`9` choose, `Enter` goes on, `?` means not sure, `-` skips.
- A card can stand for a whole sender group; tick `Mixed group` if the answer shouldn't go to all of them.
- The check of Claude's labels is *not* blind: Claude's answer stands beside each field, and you agree or
  correct it.

See also: acceptance, values, fruit

## Acceptance {#acceptance}
Guide: chapter 5, Values

How sure Jev must be before its answer counts. One slider per field. Moving it shows what that level would
give: how many of your messages get a value, and how often that is wrong on the answer key. **Serious**
mistakes, such as a person taken for a machine, are counted apart.

Nothing changes until `Apply these levels…`, which shows the difference first. **In use now** shows the
levels that apply. After a new Jev run, glance here: do the levels still give what you want?

See also: values, answer-key

## Studio {#studio}
Guide: chapter 5, Values, and the recipe "Making Talos smarter"

Like naming faces in a photo library: one card at a time, a group of mail Jev is unsure of, and one decision
settles the whole group. The counter at the top shows how many messages your decisions settled.

- **The card** is one message and its group: machine mail with the same sender and subject pattern, or a
  conversation with people. *All N* writes your answer on every message of the group; *Just this one* (the
  `.` key) on the message shown.
- **The lines** are people or machine, kind, topic and value. Jev's guess is already ticked, with how sure it
  is. `Enter` confirms every ticked line, and they become Fact. A number ticks another value, typed letters find
  any value (then `Enter`), `0` says *not this*: Jev's guess is rejected and nothing is written. `↑` `↓` move
  between lines.
- **Skip** (`→`) hides the group for a week. **Undo** (`←`) takes back your last decision; any decision in *Your
  last decisions* can be taken back.
- **The order**: groups with the most unsure mail still in your folders come first, and mail from the last 90
  days counts double. Groups that are all in Deleted Items come last, marked *all removed*.
- **Check cards** say *Checking Jev*: a message picked at random from one kind of guess (say, Jev's alerts at
  Maybe on machine mail). Answer it as any card.
- **Lifts.** Every line you confirm or correct also tells Talos how often Jev is right with that kind of guess.
  When you have said yes often enough (at least 12 times, and right at least 85% of the time even in the worst
  case), all Jev's other guesses of that kind are accepted at once, and new mail's too. *Close to a lift* shows
  how far each kind of guess has come; *Lifted* lists the lifts, each with its undo.

Tip: a card that is wrong for part of its group: choose *Just this one*, or skip it.

See also: sureness, values, fruit

## How sure {#sureness}
Guide: chapter 5, Values

Every value carries a level that says how sure it is, in words instead of a number:

- **Fact**: you decided it (100%).
- **Certain**: a rule set it, or Jev is at least 95% sure.
- **Confident**: from 85%: right almost every time.
- **Likely**: from 70%: right most of the time. Talos accepts exact values from here.
- **Maybe**: from 50%: as often right as not.
- **Doubtful**: from 30%, and **Guess** under it.

The level shows on each value in the message pane, beside the value. Mail's **Sureness** filter asks how sure
the kind, type, topic, work or personal, and keep you filter on must be: *Certain or surer* keeps only the
surest; from *Likely* down, Jev's guesses that Talos has not accepted come in too, so *kind: alert, Maybe or
surer* finds the alerts Jev guessed. A value the Studio lifted carries the level your checks proved.

See also: studio, values

## Jobs & fruit {#fruit}
Guide: chapter 5, Values, and the recipe "Making Talos smarter"

The cheapest lessons first.

- **Low-hanging fruit**: the sender groups where one decision fixes the most undecided mail. `Let Claude label
  these` makes an answer-key set and an export; tell Claude *"label set N"*, and Claude labels the groups and
  applies the values to every message in each. Ten decisions often fix tens of thousands of messages.
  `Label these` is the same, with you doing the labelling.
- **Improvement jobs**: focused Jev runs, ranked by the messages they could improve, each with its price.
  Nothing runs from here: ask Claude to run the ones worth it.

See also: answer-key, acceptance, rules, studio

## Rules {#rules}
Guide: chapter 5, Values

A rule is a condition plus a value: *"mail whose subject says DOWN, from an uptime monitor → kind: alert"*.
Rules run over the whole archive and never overwrite your own decisions. A rule beats a model and runs for
free.

- **Saved rules** say what they set, *why*, and how many messages they match. The switch turns a rule on or
  off; × removes it, and its suggestions come back.
- **Try a condition**: write conditions, `Preview` them and `Save as rule`.
- **Suggested rules** appear wherever enough messages from one sender share a value. Tick a whole category to
  make one rule from all its suggestions.

See also: values, structure, fruit

## Structure {#structure}
Guide: chapter 12, Tidying mailboxes

The plan for a tidier mailbox. Per account, every mail is tested against the structure rules from top to
bottom; the first that matches decides its place: *Talos/Automated/…*, *Talos/Keep/…*, *Talos/Areas/…*, or
*To sort*.

- Click a place to see its rules and examples, and `Why here?` to see an example's values and every rule it
  was tested against.
- **How to make it real** is the checklist from plan to mailbox, with where each step stands.
- `Prepare the next changesets` makes the next batch, planned but never committed from here.

The plan gets better by itself as the values improve: fewer mails to sort.

See also: changesets, clusters, rules

## Clusters {#clusters}
Guide: chapter 12, Tidying mailboxes

Machine mail grouped by sender, and by system when several systems share a sender. Each cluster is **dead**
(nothing in twelve months), **quiet** (less than one a month) or **active**. The cards show where the dead mail
sits and what the inbox would look like without it.

Click a cluster for examples and a cleanup, which becomes a changeset, never an instant action. Dead systems
are the safest mail to move first.

See also: changesets, structure, systems

## Changesets {#changesets}
Guide: chapter 12, Tidying mailboxes

Every change to a mailbox is a **changeset**: planned, dry-run, committed, applied, and undoable. On this page
you read the plans and dry-run them; a dry run reads each message on the server and changes nothing.
Committing and applying happen on the command line, by you or by Claude when you say so.

Apply in growing steps (1, 10, 100, all) and check the mailbox in between. Nothing is deleted for good: the
trash is the furthest any change goes, and the original stays in Talos anyway. Moving mail *out* of an inbox
always needs your explicit go.

**History** keeps every finished changeset in its status colour.

See also: structure, clusters

## Sources {#sources}
Guide: chapter 4, The archive

One card per account, its last sync and its state, and the **guarantees** Talos keeps:

- Reading never changes a mailbox: syncing opens every account read-only.
- Only you send, after confirming the account.
- Nothing is deleted for good.
- Mailbox changes are batches you approve (changesets).

`Sync mail now` fetches the mail accounts at once. The **Security** card shows who is signed in and what
happened at the door.

See also: accounts, changesets, argus

## Systems {#systems}
Guide: chapter 14, Discovery

Claude read the archive and wrote down every system, candidate binder and piece of your setup it met. Here you
say "yes, that's real".

- **Accepting** a system makes a system binder with Claude's draft as its text, or enriches the binder of the
  same name if there is one. Rejecting only records your decision.
- `Keep as retired system` keeps it for history and the dead-mail cleanup.
- The two bulk buttons accept every active system, or reject every probably-retired one.
- Keys: `a` accept, `r` reject, `e` edit before accepting, `x` select, `j` `k` move, `Enter` open.

See also: binders, clusters

## Argus {#argus}
Guide: chapter 13, Keeping watch

Argus watches the services on the Mac: disk space, Talos's sync, enrichment and web, and any other service you
add. Each either checks in and says when it'll be back, or Argus probes it. **Late**, **down** and
**failing** become macOS notifications, and so does the recovery.

> **Who watches the watcher?** The outbound heartbeat: every few minutes Argus pings a hosted check. If the Mac
> or Argus dies, the pings stop, and the hosted check emails you.

See also: events, sources
