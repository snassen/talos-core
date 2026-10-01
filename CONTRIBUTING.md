# Contributing to Talos

Thank you for wanting to help. Talos is small and opinionated, and it holds people's entire mail, so
a few things are firm. Everything else is open to discussion.

## How decisions are made

Talos has one maintainer, who reviews pull requests and decides what goes in. Disagreement is
welcome: open an issue, argue the case, show the trade-off. If the answer is still no and you need
it anyway, forking is fine and no hard feelings. For anything larger than a fix, open an issue first,
so neither of us spends a weekend on something that won't be merged.

## The promises are not negotiable

Talos sends only what the owner composed and confirmed; never deletes permanently; sync never
writes; originals are immutable; models propose and people decide; mail is untrusted input; secrets
live in the Keychain. They are spelled out in [CLAUDE.md](CLAUDE.md) and held by
`tests/test_guards.py`, which scans the source. A change that needs a guard weakened will not be
merged. If a guard trips on a harmless string, reword the string, not the guard.

## Every change comes with tests

- New behaviour gets a test. A bug fix gets a test that fails without the fix.
- **Test names are sentences** that say what must hold:
  `test_a_rule_run_never_touches_a_human_decision`, not `test_rules_3`.
- Tests use fakes for Gmail IMAP and Graph that fail on any write, and a loopback SMTP server that
  delivers nothing. No test ever talks to a real mail server or sends a real mail.

## No real mail, no personal data

**Real mail and personal data never enter the repository**: not in fixtures, screenshots, exports,
rule files, migrations, docs or commit messages. Test mail is invented (`tests/mailfactory.py`), and
so is the demo data (`scripts/demo.py`). Use invented people and `.example`/`.invalid` domains (the
owner in the tests is `owner@company.example`). Everything about one owner (their accounts, their
wording of the questions, their rules and structure, their own services) belongs in their personal
part, `$TALOS_HOME/config` (docs/install.md), never in the code; `tests/test_traces.py` fails if a
known trace of a real owner appears. If you look at your own archive while working, keep what you find
(queries, screenshots, reports) out of the repo.

## Style

- **Match the surrounding code**: its naming, its comment density, its shape.
- **Docstrings in prose that say why**, not just what. A reader should understand the decision
  behind a module from its docstring.
- **Deterministic first.** Mechanics are code and rules; a model is asked only a bounded question,
  through `talos.cases` or `talos.jev`.
- **The UI is vanilla JavaScript with no build step** (`src/talos/web/static/app.js`), and never
  uses `innerHTML`: mail is untrusted input, and a guard test enforces it. Use the CSS tokens, never
  raw colours, so dark mode works.
- **Migrations are new files** in `src/talos/sql/`, applied in name order. Never change an existing
  one, and there are no down-migrations. Put no personal data in a migration.
- Plain, friendly English in docs and UI text.

## Running the tests

The tests need PostgreSQL 18 with `pgvector`. By default they use the database `talos_test` on the
server at `host=/tmp port=5433` (set `TALOS_TEST_DSN` to use another). They create it, and truncate
its tables between tests; they never touch your real `talos` database.

```bash
uv sync
uv run pytest -q                                 # everything (a few minutes)
uv run pytest -q tests/test_guards.py            # the promises
node --check src/talos/web/static/app.js         # after any UI change
```

For UI work, use the demo database with invented mail, never your archive:

```bash
export TALOS_DSN="host=/tmp port=5433 dbname=talos_demo" TALOS_HOME=/tmp/talos-demo
uv run python scripts/demo.py && uv run talos serve --port 7421
```

## AI agents

Agents (Claude Code, Codex and others) are welcome contributors under the same rules. They follow
[AGENTS.md](AGENTS.md) (the same file as `CLAUDE.md`) and the `talos-dev` skill in
`.agents/skills/talos-dev/`, and, on an owner's own Mac, that owner's `AGENTS.md` in their personal part. If a change makes the skill untrue, update the skill in the same
commit; `tests/test_skill.py` checks that everything it names still exists.

## Security

Please don't open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).
