---
name: talos-dev
description: How to develop Talos, a local mail and Teams command center that runs on its owner's Mac (Python, PostgreSQL on port 5433, Starlette and vanilla JS). Use for any feature, bug fix, refactor, UI change, migration, rule or taxonomy change, or operational task in Talos or Talos Web, and whenever a session touches that repository, even if it starts elsewhere. Covers the working loop (orient, change, test, check it live, commit, restart), the UI and backend conventions, the live services and logs, the known pitfalls, and what needs the owner's explicit go.
---

# Developing Talos

Talos gathers the owner's mail (Microsoft 365, Gmail, iCloud and other IMAP accounts) and Teams into one archive
on their Mac, keeps the originals immutable, and builds metadata, binders, work items and views on top. **Talos
Web** (127.0.0.1:7420, and over Tailscale) is where they work. Explain outcomes in plain words, and make
decisions that are reversible on your own.

**The owner's own rules come first.** When `AGENTS.md` exists in the personal part (`uv run talos config path`
prints the folder; usually `~/TalosData/config`), read it before anything else: it holds their hard lines for
their instance (which accounts may be written to and when, the neighbours on their machine, their budget). Its
rules apply on top of this skill's.

The repo's `AGENTS.md` (the same file as `CLAUDE.md`) holds **the promises** and the **code layout**. Read it
first in every session, and `docs/architecture.md` when you need the layer model. This skill holds the
**working practice** around them.

## The loop

1. **Orient** (a few minutes, not an expedition). Read AGENTS.md. Then `git log --oneline -15` and
   `git status`. Find the code with `grep -n` in `src/talos/` and the view in `src/talos/web/static/app.js`
   (one big file; search for `async function view<Name>` or the route in `web/app.py`). The feature docs in
   `docs/` say why things are the way they are (`design.md`, `workspace.md`, `enrichment-plan.md`,
   `mailbox-structure-plan.md`, `argus.md`, `studio.md`, `writeback-test-plan.md`…). The owner's own
   configuration and the reports mined from their mail live in `~/TalosData/config` and `~/TalosData/docs`
   (talos.personal).
2. **Change**, in the style of the surrounding code: the same comment density, the same naming, prose
   docstrings that say *why*. Deterministic first: a model is asked only a bounded question, through
   `talos.cases` or `talos.jev`.
3. **Test**:
   - New behaviour gets a test whose name is a sentence.
   - A bug fix gets a test that fails without the fix.
   - Run the tests near the change first. Run the full suite (`uv run pytest -q`, about 4.5 minutes, needs
     PostgreSQL on 5433) before committing anything non-trivial.
   - After any UI change, run `node --check src/talos/web/static/app.js`.
   - `scripts/verify.sh` in this skill does the quick set.
4. **Check it live.** Restart the web service (`scripts/restart-web.sh`). Then check the actual page: the
   in-app browser if you have one, otherwise headless Chrome through Playwright
   (`references/ui.md` § Checking a page). Look at the real archive through read-only queries only.
5. **Commit and push**:
   - Commit to `main` with a message that says what changed for the owner, then `git push`.
   - Larger work may go on a branch or worktree, merged with a "Merge …" commit.
   - When two branches both add a migration, renumber one on merge (the files apply in name order).
6. **Write it in the release notes**: every change the owner would notice gets a line under `## [Unreleased]` in
   `CHANGELOG.md` (Added, Changed, Fixed, Removed, Security), in plain words, in the same commit. When a
   batch of work is done, cut a release with `scripts/release.py minor` (features) or `patch` (fixes only),
   then `git push && git push --tags`. Versions follow Semantic Versioning; before 1.0 only minor and patch.
7. **Keep the skill true**: if the change touched anything this skill describes, update it in the same
   commit (see below).
8. **Tell the owner what changed**: what they'll see, what they need to do (usually reload), and what you
   verified. If they'll ask again, write it into the relevant `docs/*.md`.

## Hard lines

These go beyond AGENTS.md's promises, and the owner's own AGENTS.md (above) adds to them. Stop and ask before
crossing any of them:

- **Sending mail or Teams posts.** Only the owner's Send plus their confirmation (send.py alone; a Teams post
  goes on their Enter in that conversation's box, which is its confirmation). Never add a path that sends, and
  never press Send, "Yes, send" or Enter in a Teams box yourself, not even to test.
- **Moving mail out of any inbox** (archive) needs the owner's explicit go, every time.
- **Microsoft 365 (Graph) write-back** is switched on only for one agreed use at a time, with the owner's go
  (docs/writeback-test-plan.md): `writeback_enabled` on the account for the run, and off afterwards.
  Gmail write-back is only for additive `Talos/…` labels through committed changesets.
- **Calendar write-back** is for entries the owner saves in Talos Web, through `calwrite` alone: never an
  attendee, never a series, trash at most (docs/calendar.md). Nothing automated writes to a calendar, and
  removing an iCloud entry (permanent there) needs their go first.
- **Never delete permanently.** Trash is the furthest. Don't delete the owner's objects or work items unless
  they ask.
- **The door** (docs/security.md): every route needs a session (the owner's password and an authenticator
  code). Never add an exception to `web/gate.py`'s public paths for anything that returns mail, never weaken
  the read budget, and never ask for their password or codes: `talos web setup` is theirs to run.
- **Secrets** live in the Keychain and the owner types them in. Never ask them to paste one into a chat, and
  never print one.
- **Talos's database is on port 5433.** Leave any other PostgreSQL on the machine alone.
- **Real mail never enters the repo**: no fixtures, screenshots or exports of it. Test mail is invented
  (`tests/mailfactory.py`). Anything that shows real mail (manuals, reports) goes in `~/TalosData/`, and so
  does anything about the owner: their identity, accounts, rules, structure and discovery drafts live in
  `~/TalosData/config` (talos.personal); the repository holds examples. `tests/test_personal.py` fails if a
  known trace of the owner comes back.
- **Cost.** Jev (the hosted model) is paid. Incremental enrichment is capped by `~/TalosData/enrich.json`'s
  daily budget. Ask before a run that costs more than a few dollars.

## Keeping this skill true

Claude Code and Codex both read this skill, and either may change the code under it, so treat it as code.
- **Same commit.** A change that makes anything here untrue (a renamed function, a moved file, a new
  service, a new convention, a pitfall you just hit) updates the skill in the same commit.
- **The test.** `tests/test_skill.py` fails when a file, function, constant, CSS token or marker named here
  is gone. It runs in the full suite, so a stale skill shows up before the commit.
- **What it can't check.** The test cannot judge advice or prose. After a large change, reread the section
  it touches.
- **Keep facts out.** Facts that change often (counts, dates, thresholds in use) belong in `docs/` or in the
  code, not here. This skill holds practice, and points to where the facts are.

## References (read when the task needs them)

- `references/ui.md`: app.js building blocks (`h()`, `api()` and its cache, `render(keepFocus)`, the pane),
  CSS tokens, guard tests, and how to check a page headlessly.
- `references/backend.md`: web handler pattern, migrations, the test fixtures, the insight cache, stored
  Messages selections, CLI commands.
- `references/operations.md`: the launchd services, logs, the data folder, read-only database queries, sync
  and enrichment, Argus.
- `references/pitfalls.md`: things that have already bitten, so they don't bite twice.

## Scripts

- `scripts/verify.sh [pytest args]`: `node --check` on app.js, the guard and web tests, plus any tests you
  name. With `--full` it runs the whole suite.
- `scripts/release.py X.Y.Z | minor | patch`: moves Unreleased in CHANGELOG.md under the new version, sets
  `talos.__version__` and pyproject.toml, commits those three files and tags vX.Y.Z.
- `scripts/restart-web.sh`: restarts the web service (`<service_prefix>.web`, owner.json) and waits until it
  answers.
