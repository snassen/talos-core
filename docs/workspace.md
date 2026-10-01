# The work space and Discover

Built from a concept sketch: a command
center, a binder as a living page, and Discover. Deterministic first: everything here is a query over
the archive and its metadata. No model is asked, and no mail server is touched.

## Work space (`#space`, `talos.space`)

- **The map.** Each area is a card with the binders linked to it: projects, systems and topics. A binder is
  linked by a `related` edge (the vault import made these) or a `member_of` edge ("Part of", or "In area"
  when a binder is made here), in either direction. Each card shows what is overdue, blocked and open, and how much
  mail mentioned its binders in the last 30 days. Binders in no area are listed under "Still to place".
- **Needs you.** Overdue, blocked and focused work (talos.work's attention), and watchers with news.
- **Watchers.** A watcher is a saved Messages selection: the view's own query string, so opening one
  shows exactly what it counts. It counts all matching mail, and what arrived since you last looked.
  It may belong to a binder. "Take in" adds its new mail to the binder and marks it seen. Only you
  can do that; it never happens by itself. Make one with "Watch" in Messages, from a binder, or here.

## A binder's page (`#objects/<id>`)

Below the About section and the Board, three panels are added:

- **Found in your mail.** Threads that mention the binder's search terms and are not in it yet.
  Threads you took part in come first, then people's, then the rest, newest first. Each is Added
  (objects.add) or marked "Not this" (objects.exclude).
  The terms are `attrs.terms`, or the binder's name when it has none. Each term is a phrase: "Check Point"
  matches the words together, not check and point anywhere. A term matches in the text, the subject or the
  sender's address.
- **Neighbours.** Binders linked to this one, and binders whose terms turn up in three or more of the same threads.
- **Watchers** on this binder.

The mail counts per binder and the shared threads are computed together (one phrase search per
binder, about 3 s) and kept in `insight_cache` as `space-binders`. They are refreshed behind the page
when new mail arrives or a binder changes.

## Discover (`#discover`, `talos.discover`)

Insights, computed together and kept in `insight_cache` as `discover` (about 13 s, refreshed behind the page):

| Insight | What it is |
|---|---|
| Forgotten | People who asked you something in the last 60 days, unanswered (talos.importance's waiting) |
| Recurring | Renewals, licences and expiries from the same sender in the same month, two years or more, for the next 3 months. A sender that writes in more than 6 months of the year is a report, and is left out |
| Cross-connection | Organisations you wrote to in 3+ threads this year that no binder names (name, terms, description, text) |
| Gone quiet | Automated senders that wrote regularly (12+ a year) and then stopped for 4× their usual gap. Marketing is left out |
| New | People who first wrote in the last 45 days, and whom you have answered |

**Saved aggregations** are a Messages selection counted in groups (sender, domain, year, month,
account), counted when shown. Claude's first six came with migration 025. You save your own with
"Aggregate" in Messages.

Dimensions the filter bar has no menu for (ask, …) travel in `S.f.dims` and show as chips.
