# Discovery drafts

Three inventories for the owner to **confirm or reject**: the systems of their workplace, their own setup, and
candidate areas, topics and projects. They are mined from the owner's mail, Teams and machine, so they live outside
the repository (talos.personal):

| Draft | JSON for import | Report |
|---|---|---|
| The workplace's systems | `TALOS_HOME/config/discovery/systems.json` | `TALOS_HOME/docs/discovery/workplace-systems.md` |
| The owner's own setup | `TALOS_HOME/config/discovery/my-setup.json` | `TALOS_HOME/docs/discovery/my-setup.md` |
| New areas, topics, systems | `TALOS_HOME/config/discovery/candidates.json` | `TALOS_HOME/docs/discovery/new-areas-topics.md` |
| Collectors | – | [collectors-plan.md](collectors-plan.md) (in the repository) |

The full account of how the drafts were made, and what still needs the owner's input, is in
`TALOS_HOME/docs/discovery/README.md`. The drafts were produced read-only: SELECT-only
queries in read-only transactions, read-only commands, and reading (never writing) the Obsidian vaults.

## How the owner decides

Each JSON item carries two empty fields:

```json
{
  "key": "unifi",
  "kind": "system",
  "name": "UniFi",
  "status": "active",
  "...": "evidence, examples, collector, questions",
  "decision": null,
  "correction": null
}
```

- `"decision"`: `null` (not yet decided), `"accept"` or `"reject"`.
- `"correction"`: `null`, or an object with the fields the owner wants changed, for example
  `{"status": "probably_retired", "name": "UniFi (office)", "note": "the Cloud Key was replaced by a UDM Pro"}`.

The owner can edit the JSON directly, or tell an agent "accept 1–12, reject 30, UniFi is a UDM Pro", and the agent
writes the decisions. Nothing is created until the owner accepts it on the Discovery page (or a load takes a decision from the file).

### The review and the import (built: Operations › Discovery)

`talos discovery load` reads the three files into the table `discovery_item` (migration 022). Loading
is idempotent: the drafts are refreshed, a decision already made is never overwritten, and a decision
written into a file by hand is taken only for an item without one. The table is the source of truth;
`talos discovery export [--out DIR]` writes the decisions back into the files (nothing else changes).

The page Operations › Discovery has a tab per file, filters (status, category, undecided), the
progress ("34 of 107 decided"), and the draft in the reading pane with Accept, Reject and Edit before
accepting (keys a, r, e; j/k or the arrows move; x selects). What a decision does (`talos.discovery`):

- **A system** accepted becomes an `object` of kind `system`: name and description from the draft;
  the body has Purpose, How it's used, Evidence, Related systems, How Talos could document it and
  Open questions; attrs hold status, vendor, category, first and last seen; origin
  `{"uid": "discovery:<key>"}`, so accepting twice makes nothing new. A system binder that already
  exists (by `existing_object_id`, or by the same name with or without its parenthesis) is linked and
  enriched instead: the draft becomes a note on it and only its empty attrs are filled.
- **A candidate** becomes a binder of its kind, nested under its suggested parent when a binder of
  that name exists.
- **A setup item** becomes a line in a note (one per group) on the topic binder "My setup (<host>)", named after the Mac.
- **Reject** stores the decision only. For a system the owner can instead choose **Keep as retired system**:
  a binder with lifecycle retired, for history and the dead-mail cleanup. "Reject all probably-retired
  systems" marks them retired and makes no binders; "Accept all active systems" accepts the undecided
  active ones.

Still to come: the cleanup table as rules (one per dead system), and rejected items remembered by a
later discovery run.

## How the evidence was gathered

- **Senders and domains**: inbound work mail grouped by sender domain and by internal robot address
  (for example itrobot@, alert@, backupalert@, a backup product's or a firewall's own address), with counts and
  first/last dates.
- **Subjects**: per system a sender pattern and a subject pattern (`scratchpad` scripts, not committed);
  counts split into *from the system*, *subject mentions* and *sent by the owner*; an extra "operational" date from
  subjects about invoices, licences, renewals, alerts and backups.
- **Teams**: the same system patterns over chat text, and named group chats with volume.
- **Existing objects**: the `system` objects, their bodies and the vault binders; any roster the owner keeps
  of their own agents and tools.
- **Personal**: the topic assignments Talos already holds for Gmail, plus vendor clusters (music gear,
  photography, creators).
- Only counts and short subject phrases are quoted. Colleagues appear by role or not at all.

Caveats: subject and chat patterns over-count common words a little (sender counts are exact); "last seen"
can be vendor marketing, so dead systems also carry the date of the last mail from their own senders; statuses are judgements
from mail, which cannot see a system that runs silently.
