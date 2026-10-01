# Talos: the backend

## Web routes (`src/talos/web/app.py`)

Everything lives inside `create(settings, …)`, and the routes are listed near the end.
- **A GET handler** is sync: `with conn() as c: return JSON(module.fn(c, …))`.
- **A POST handler** is `async`. It starts with `if refused := _refused(request): return refused` (the
  `X-Talos` header check), reads `body = await _body(request)`, writes, `c.commit()`s, and turns the module's
  `…Error` (a `ValueError`) into a 400.
  - `_space_write(request, fn)` is a compact helper for this.
  - Sending uses `_refused_send`, which also demands a same-origin `Sec-Fetch-Site`.
- **Injectable dependencies** are keyword arguments of `create()`, so tests pass fakes: `dry_runners`,
  `send_transport`, `sync_runner`, `argus_notifier`…
- **Messages filters from a query string** go through `search.filters_from(get, getlist)`. For a stored
  string, use `search.filters_from_query(qs)`: watchers and aggregations store the Messages view's own query
  string.
- `search.messages_sql(q, **filters, select="…")` gives "select … from the matching messages m". Wrap it as a
  subquery to count or group.

## Slow answers: the insight cache

Anything over roughly a second on the real archive (clusters, unlock, Discover, space-binders) goes through
`insight.get(conn, NAME, fingerprint_fn, compute_fn, dsn=…, first_behind=True)`.
- The page gets the stored answer at once, plus `stale`/`refreshing`.
- The answer is recomputed in a thread when the fingerprint changes.
- The fingerprint is cheap: max ids, `insight.table_writes(...)`, the date.
- The UI side is `pollWhileRefreshing`.

## Migrations

- New files go in `src/talos/sql/NNN_name.sql`, applied in name order.
- Nothing is ever changed in an existing file, and there are no down-migrations. Start with a comment saying
  what it adds and why.
- Apply on the real database with
  `uv run python -c "from talos import config, db; s=config.load(); c=db.connect(s.dsn); print(db.migrate(c)); c.commit()"`,
  or `uv run talos setup`.
- A new table must go in `TABLES` in `tests/conftest.py`, which truncates between tests. Seed rows from a
  migration are truncated there too.
- Never put names from the owner's archive in a migration. Seed personal data with a script run on their database
  instead.

## Tests

- **Fixtures** (`tests/conftest.py`): `database`, `conn` (accounts gmail, work and local; the owner's invented
  addresses in `my_address`), `ingestor`, `vault`, `taxonomy_loaded`.
- **Helpers**:
  - `tests/mailfactory.py`: `mf.make(frm=, to=, subject=, body=, date=, msgid=, headers=)`, where `mf.ME` is
    the owner's invented Gmail address.
  - `ingestor.ingest(account, raw, Location(folder, key, labels=, provider_thread_id=, received_at=))`.
  - `test_messages_rules._mail(...)`, `_archive(...)` and `NOW`.
  - `test_web.client(database, vault)` is a TestClient.
- **`tests/test_guards.py`** scans the source for the promises: no innerHTML, a single sender, no expunge,
  read-only sync, no secrets. Don't weaken it. If a guard's wording trips on a harmless string, reword the
  string.
- The fakes for Gmail IMAP and Graph fail on any write. `smtpfake.py` delivers nothing.

## CLI (`talos …`, `src/talos/cli.py`)

- **Daily running:** `setup`, `status`, `sync [ACCOUNT…] [--then-rules]` (one run per account at a time, with
  file locks in `~/TalosData`), `serve`.
- **Values:** `rules`, `events`, `importance`, `taxonomy load`, `enrich …` (the pre-pass, Jev runs,
  `jev new`), `cases`.
- **Mailbox changes:** `changeset` (plan, dry-run, commit `--max N`, apply, undo), `structure` (plan, report,
  why, changesets).
- **Other:** `discovery`, `export`, `vault import`, `argus …`, `launchd` (prints plists, never installs them).

## Code map shortcuts

- **Watchers, binders' mail, work space:** `space.py`. **Discover and aggregations:** `discover.py`.
- **Sync now:** `syncnow.py` (and `TeamsFast`, the Teams fast lane).
- **Teams place:** the `/api/teams/…` routes in `web/app.py`, `search.py`'s `chat`/`team`/`channel` filters and
  `thread(around=, after=)`, posting in `send.py` (`TeamsPost`, `Sender.teams_confirm`/`teams_send`).
- **Calendar:** `calendars.py` (calendars, entries, the copies) with `sources/m365calendar.py`,
  `sources/icloudcalendar.py`, `sources/googlecalendar.py`; writing back in `calwrite.py`, Google sign-in in `googleauth.py`;
  docs/calendar.md. **Timeline (Gantt):** `timeline.py`; docs/timeline.md.
- **Tune › Jobs & fruit** (low-hanging fruit, improvement jobs): `unlock.py`. **Answer keys:** `gold.py`.
- **Tune › Studio** (grouped decisions, calibration, lifts): `studio.py`; the levels of sureness and Mail's
  `sure=` filter: `sureness.py`; docs/studio.md.
- **Structure:** `structure.py` with `rules/structure.json`. **Rules:** `rules.py` with
  `rules/talos-rules-*.json`.
- **Taxonomy:** `rules/taxonomy.json`.
