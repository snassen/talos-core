# Talos: running it

## Services (launchd, user agents)

The labels start with owner.json's `service_prefix` (default `local.talos`); `talos where` lists the ones loaded.

| Label | What | Notes |
|---|---|---|
| `<prefix>.web` | `talos serve` on 127.0.0.1:7420 | KeepAlive. Restart: `scripts/restart-web.sh` |
| `<prefix>.sync` | `talos sync --then-rules` every 300 s | Then runs rules, events, importance, the structure plan, the incremental Jev hook, and the nightly backup |
| `<prefix>.teams` | `talos sync teams --recent 20` every 20 s | The Teams fast lane. Quiet: no sync_run row when nothing was seen; checks in with Argus as `talos-teams` every 4 minutes. `talos launchd --teams` prints it |

- **Tailscale:** `tailscale serve --https=8443` puts it at https://<machine>.<tailnet>.ts.net:8443 (the name is in `~/TalosData/web.json`). A middleware
  checks `Tailscale-User-Login` against `~/TalosData/web.json`.
- **PostgreSQL 18** is a Homebrew service on port **5433**, with database `talos`; the tests use
  `talos_test` (or the database TALOS_TEST_DSN names). Leave any other PostgreSQL on the machine alone.

## The data folder `~/TalosData` (TALOS_HOME)

`talos where` maps all of it (and the code, the database, the Keychain by name, the services and the backups),
each with why it lives there; `talos where --write FILE` as Markdown. Use it first when something is not
where you expect.

- `vault/`: the originals.
- `config/`: the owner's own configuration, kept out of the repository (talos.personal): `owner.json`,
  `~/TalosData/config/accounts.json`, `recipient.txt` and `questions.json` (what Jev is told about the owner
  and their wording of the questions; the exact text is part of every question version), `taxonomy.json`,
  `~/TalosData/config/structure.json`, `rules/`, `argus-services.json`, `discovery/` (the drafts) and
  `AGENTS.md` (their rules for agents). The repository has examples in `rules/`. Change these files, not the
  examples, when the owner's setup changes.
- `docs/`: reports mined from the owner's mail (the discovery reports, the rules report, dated status reports).
- `backups/`: the nightly backup of the owner's own work (talos.backup, docs/backup.md).
- `logs/`: `talos.log` has everything (moved aside at 10 MB, five kept); `launchd.err` and the other `*.err`
  files have warnings and errors only; `launchd.out` (sync summaries), `web.out`, `sync-now.out`, and the
  reports of runs.
- `enrich.json`: the incremental enrichment switch and budget.
- `argus.json`: the Argus timers.
- `web.json`: the Tailscale identities let in.
- `*.lock`: one per sync account, plus rules and enrichment. A held lock means a run is going; don't delete it.
- `manual/`: the field guide's source and build scripts. The PDFs sit next to it.

## Looking at the real archive (read-only)

- **SQL:** `psql -p 5433 -d talos -c "select …"`. Useful tables:
  - `message` (direction `in`/`out`/`self`; `self` is mail between the owner's own addresses);
  - `message_location` (labels and folders on the server);
  - `assignment` and the views `effective_message_assignment` / `effective_assignment`;
  - `object`, `edge` (rels `member_of`, `related`, `excluded_from`), `work_item`;
  - `sync_run`, `changeset`, `structure_plan`, `watcher`, `aggregation`, `insight_cache`.
- **Python:** `uv run python -c "from talos import config, db, …; s=config.load(); c=db.connect(s.dsn); …"`
- **The API** needs a session. For a quick look, prefer SQL or Python. If you must use the API, make a
  session file (`uv run talos web session --cookie-file <scratch>/state.json`) and send its cookie. The
  token is in the file: never print it. Without a session you get 401, which is the door working.
- Writes to the owner's database belong in reviewed code paths. One-off fixes such as seeding watchers or setting
  binder terms are fine when reversible. Say what you did.

## Sync and enrichment

- **Mail** syncs every 5 minutes. **Sync now** (`POST /api/sync`) runs the mail accounts (never Teams) as a
  separate process and reports per account.
- **Teams** chats and channels sync with the 5-minute job (channels since Team.ReadBasic.All and
  Channel.ReadBasic.All are in the read scopes). The fast lane (`talos sync teams --recent 20`,
  about two seconds) runs every 20 seconds as `<prefix>.teams`; its last look is in sync_cursor
  (teams, fast), which the Teams page reads. If the service has stopped, the open page starts a look itself
  (`syncnow.TeamsFast`). A post in a channel reads that channel again at once (`--channel TEAM_ID/CHANNEL_ID`).
  A post also boosts the lane (`TeamsFast.boost`): for `TEAMS_BOOST_FOR` seconds the page asks often and the web
  server starts a look every `TEAMS_BOOST_EVERY` seconds where the owner posted, then it cools down by itself.
  Output: `~/TalosData/logs/teams-fast.out`.
- **Jev** (TypeSafe, key `typesafe-api-key` in the Keychain) judges new mail every 15 minutes within
  `enrich.json`'s daily budget, and checks in with Argus as `talos-enrich`.
- **Acceptance levels in use:** the boundaries at 0.90, kind at 0.85, other exact fields at 0.70.

## Argus

- `talos argus …` and the Argus page show the services with their check-ins and probes.
- The outbound heartbeat URL is in the Keychain as `argus-heartbeat-url`. The check-in token is
  `argus-checkin-token`.
- The owner's other services (argus-services.json in the personal part) are registered with the rest; one may
  stay paused until the owner says to wire it in.
- A service a launchd job runs is judged by restarts (pid changes), not by the last exit code.

## Mailbox changes (changesets)

- The steps are plan, dry run (read-only), commit `--max N` (1, then 10, 100, all), apply, and undo if needed.
- Gmail executors add only `Talos/…` labels, Inbox, Trash and Starred.
- Graph (Microsoft 365) is switched on per agreed use and off afterwards (docs/writeback-test-plan.md). Its dry run reads
  with the read-only token; `talos changeset check ID` reads back where the messages are after apply;
  `reconcile` then `retry` mend an apply that broke half-way.
- Archiving out of an inbox needs the owner's explicit go.
