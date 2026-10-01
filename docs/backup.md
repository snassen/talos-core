# Backups

Talos keeps three kinds of things, and each is protected differently:

| What | Can it be made again? | Protected by |
|---|---|---|
| The originals (TALOS_HOME/vault) | No: they are the source | Your own backup of the data folder (Time Machine or similar) |
| Derived values in the database (messages, threads, rule and model values, events) | Yes: `talos reparse`, `talos rules run`, a sync and the Jev jobs rebuild them from the vault | The vault |
| Your own work in the database (binders, work items, notes, hand-written rules, answer keys, Studio decisions, changesets, values you set) | No | The nightly backup below, and your backup of the database folder |

## The nightly backup

The first full `talos sync --then-rules` after 02:00 writes `TALOS_HOME/backups/YYYY-MM-DD/`:

- `own.dump`: the tables that hold only your work, schema and data, as a `pg_dump -Fc` archive
  (the list is `talos.backup.OWN_TABLES`);
- `own-values.jsonl.gz`: every value you set (and every one a Studio lift accepted), one JSON line each, with the
  message's `raw_sha256` (its file in the vault) and its Message-ID beside the database id;
- `own-links.jsonl.gz`: the links you made or imported (binder memberships, a work item's mail), keyed the same way;
- `manifest.json`: when, the Talos version, the migrations applied, and how many rows of each part.

The folder and files are readable only by you. The newest 14 days are kept, never fewer than 3. Argus watches it as
`talos-backup` and fails it when a day passes without one.

```bash
talos backup            # make one now (replaces today's)
talos backup --status   # the latest, and the days kept
```

## Restoring

Stop the services first, so nothing writes while you restore:

```bash
launchctl bootout gui/$(id -u)/local.talos.sync   # your service_prefix (owner.json), if not the default
```

`talos where` lists the services. Start them again with `launchctl bootstrap` when done.

### One table, in the same database

Put back a single table, for example the notes, from a day's dump. Empty only that table first, never with
`cascade`: in the live database, a cascade would also empty the mail that refers to it.

```bash
pg_restore -l ~/TalosData/backups/2026-10-01/own.dump | grep "TABLE DATA public note "
psql "host=/tmp port=5433 dbname=talos" -c "delete from note"
pg_restore -d "host=/tmp port=5433 dbname=talos" --data-only -t note ~/TalosData/backups/2026-10-01/own.dump
```

### Everything, into a database restored from the database folder's backup

When the database folder came back from an older backup, the dump is newer than it. Restore into a copy first and
compare, or put back the tables you need one by one as above.

### Into a database rebuilt from the vault

Every id differs in a rebuilt database, so `own.dump` cannot be laid over it. The values and links can: each line
names its message by `raw_sha256`, which the rebuild gives the same message again.

```python
import gzip, json
for line in gzip.open("own-values.jsonl.gz", "rt"):
    v = json.loads(line)
    # find the message: select id from message where raw_sha256 = v["target"]["raw_sha256"]
    # (for a thread, the thread of that message), then insert the assignment with v's value
```

Binders, work items and notes need their own rows back from `own.dump` (restore it into a scratch database and copy
them across), and then their links from `own-links.jsonl.gz`.
