# The Studio and the levels of sureness

The Studio works like naming faces in a photo library: approve or reject a guess, add a value that is not yet
known, and each answer unlocks many more recognitions, with a counter that shows how much you settled. The levels
of sureness only name spans of confidence: 100% is a fact, and 50% is … well, maybe.

Code: `talos/studio.py` (the pool, cards, decisions, calibration, lifts), `talos/sureness.py` (the levels),
migration `030_studio.sql`, the Studio view in `app.js` (Tune › Classify › Studio), and `sure=` in
`search.messages_sql`. Tests: `tests/test_studio.py`.

## Why

After the backfill, the focused runs and the combined run, Jev's unsure answers stopped moving: asking again gives
the same hesitation. What still moves them is a person. But a person labelling message by message is slow, and the
answer key only measures, it does not decide. The Studio makes your decisions count many times over, in two ways:

1. **By the group.** Machine mail repeats: one sender's same notice, hundreds or thousands of times (for example a
   CI service's *Deployment review* notices, a firewall's alerts, a backup monitor's reports). One decision on a card
   is written on every message of its group.
2. **By calibration.** Every line you confirm or correct is also a verdict on Jev's guess of that kind. Once your
   verdicts show that Jev is right there often enough, the rest of Jev's guesses of that kind are accepted at once:
   a *lift*.

## The levels

| Level | From | Meaning |
|---|---|---|
| Fact | 100% | You decided it (source human) |
| Certain | 95% | A rule (or the pre-pass) set it, or Jev is at least 95% sure |
| Confident | 85% | Right almost every time |
| Likely | 70% | Right most of the time; Talos accepts exact values from here |
| Maybe | 50% | As often right as not |
| Doubtful | 30% | More often wrong than right |
| Guess | 0% | A shot in the dark |

A rule's value counts as 0.97, an import's as 0.75, and a model value with no confidence (Claude's answer-key
labels propagated to groups) as 0.75: Likely. A lifted value carries the calibrated confidence, so its level is
the level your checks proved (Confident or surer), with Jev's own number kept in `evidence.jev_confidence`.

Where the levels show: on every value tag in the message pane (a small word after the value), on each line of a
Studio card, and in Mail's **Sureness** filter (`sure=`). The filter applies to the value filters (kind, type,
topic, work or personal, keep, and any `dim=`): a value counts only at that level or surer; and where a message's
field has no value that counts, Jev's proposal counts when it is that sure. So *Maybe or surer* reaches into what
Jev guessed but Talos has not accepted. The sidebar's facet counts still count only the values that count.

## The pool and the cards

`compute_pool()` looks at every incoming e-mail and, per field (people or machine, kind, topic, value), whether it
is *settled*: your or a rule's value, or Jev's accepted value at Confident or surer. Messages group as the pool
sees them: machine mail by subject pattern (`message_pattern`: sender plus subject skeleton), people's mail by
thread, anything else alone. Groups rank by the unsettled field-values of their mail still in your folders (not in a trash
folder: search.TRASH_FOLDERS), with mail from the last 90 days counting double; then by all their unsettled
field-values (otherwise the biggest groups can be dead systems whose mail a prune has already moved to Deleted
Items). It takes seconds on a large archive (well over 100,000 incoming e-mails), so it is cached (`insight_cache`
'studio-pool') and recomputed behind the page when your decisions or Jev's runs move the values. It keeps the top 5,000 groups, and
the *cells*: Jev's open proposals counted by field, value, level and people-or-machine.

A card (`card()`) reads the group's newest 400 messages: each line shows the representative's value (or the
group's commonest), its source and level, how many of the 400 are settled, and Jev's alternatives (the scores
stored with Jev's proposal). The page shows every value of a short list (people or machine, kind, value) and Jev's
alternatives plus typed search for topic.

**Check cards** come every third card when a cell is worth it: the cell with the most messages per verdict still
needed to lift, a random message of it from a sender the cell has not been checked on, shown as its group's card.

## Deciding

`decide()` takes your lines: a value (your choice), or null (*not this*: Jev's proposal is rejected, nothing is
written). Scope *group* writes on every incoming e-mail of the group (up to 50,000), *one* on the representative.
Your values are human assignments (`source_ref 'studio:<decision>'`, `evidence.studio`), so they beat everything
and a rerun of Jev never touches them. A topic also writes its sphere, and a value its keep side, as yours, as the
answer-key groups do. `settled` counts the messages that got a value of yours they did not have.

After a decision (or its undo) is committed, the web route calls `replan()`: the messages are placed again in the
structure plan (structure.refresh_messages, about 1 s for some 10,000 messages). It runs after the commit because the
planner rolls back on a failure, and must never take your decision with it.

`undo()` takes a decision back: its rows become superseded (`undone:studio:<id>`), the human rows it superseded
come back, and the proposals it rejected are proposals again. A skip hides a group for 7 days; undo brings it back.

## Calibration and lifts

A verdict (`studio_verdict`) is written for each decided line whose representative had a Jev value: the cell
(field, Jev's value, its level, people or machine), whether you agreed, and what you chose. Only verdicts on
*proposals* can lift (verdicts on accepted values are shown, for how right the accepted levels are).

A cell lifts when it has at least 12 verdicts and the lower bound of its agreement (Wilson, one-sided 95%) is at
least 0.85, Confident: sixteen yes in a row lift a new cell; one no in the first twenty keeps it waiting longer.
`lift_cell()` then accepts every open proposal in the cell (Jev's value at that level, people or machine as the
cell, on a message whose field has no value that counts): status active, `decided_by 'studio-lift:<id>'`,
confidence the calibrated lower bound. `studio_lift` keeps every row it accepted, and `undo_lift()` returns them to
proposals with Jev's own confidence.

**New mail** gets the same treatment: after every `talos sync --then-rules`, `relift()` runs each lifted cell again
over the proposals made since its last lift (the incremental Jev hook's answers). The lifts are not policy rows
(`decided_by` does not start with `policy:`), so `talos enrich accept` neither demotes nor re-stamps them.

## Keys

`Enter` confirm · `1`–`9` tick a value on the focused line · letters find a value, `Enter` picks the first ·
`0` not this · `↑` `↓` (or Tab) lines · `.` all of the group or just this one · `→` skip · `←` undo. The place
numbers, `[` `]` and `/` stand aside while the Studio is open, as in the answer key.

## What is next

- The sidebar's facets could follow the Sureness filter (count proposals too).
- Teams windows as cards (their units are two-hour windows, not patterns).
- ~~Kind derived from type where the direct kind is unsure~~: done (backfill.kind_from_type, run by
  accept).
