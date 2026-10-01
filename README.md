# Talos

A local command center over your own mail. Talos ingests every message from Gmail, Microsoft 365,
IMAP accounts and Teams, keeps the originals untouched, and builds light, fast
objects on top: people, organisations, threads, attachments with their text, events from
machine mail, and any grouping you define. Rules are data, decisions record where they came
from, and changes to mailboxes go back to the servers only as reviewed batches.

It sends a mail only when you press Send on one you composed and confirm the account it goes
from; nothing automated ever sends. It never deletes anything permanently.

The design is in [docs/architecture.md](docs/architecture.md); the rules for changing the code
are in [CLAUDE.md](CLAUDE.md).

## Start

The full walk-through, including how to set up your personal part (in a private repository of your own,
or only on your Mac), is in [docs/install.md](docs/install.md). In short:

```bash
brew install postgresql@18 pgvector       # port 5433 (see CLAUDE.md)
brew services start postgresql@18
uv sync
uv run talos setup                        # database, migrations, accounts
```

Your own configuration lives outside the repository (`talos config init` makes it; `TALOS_CONFIG` or
`~/TalosData/config`; talos.personal): the accounts
(`accounts.json`, see `rules/accounts.example.json`), the paragraph Jev is told about you (`recipient.txt`),
your rules (`rules/`), and the discovery drafts (`discovery/`). Without them Talos runs on the examples in
`rules/`.

Secrets go in the macOS Keychain, typed by you:

```bash
security add-generic-password -U -s talos -a gmail:you@gmail.com -w               # Gmail app password
uv run talos auth graph work                                                         # Microsoft: device code sign-in
```

Then:

```bash
uv run talos sync gmail --limit 500       # a first read-only slice
uv run talos sync                         # continue; resumes where it stopped
uv run talos rules run && uv run talos events run
uv run talos serve                        # http://127.0.0.1:7420
```

`uv run talos launchd` prints the job that runs the sync every five minutes; it is not
installed automatically.

## What works today

The release notes are in [CHANGELOG.md](CHANGELOG.md) (and under the version number in Talos Web's rail).

| Area | State |
|---|---|
| Vault, schema, parser, ingest | Done: people, orgs, threads, attachments (PDF, Excel, EXIF), Swedish + English search |
| Sync: Gmail, Graph mail (delta), IMAP, Teams chats and channels, .eml/.emlx import | Running every 5 minutes; Teams also every 20 seconds |
| Values: rules as data, your decisions, Jev (backfill, focused and combined runs, new mail) | Running; accepted at named levels of sureness |
| Tune: answer keys, acceptance, jobs, Studio (grouped decisions and lifts) | Done |
| Work: board, list, timeline; binders and areas; calendar (Microsoft 365, iCloud, Google) | Done |
| Writing mail, Teams posts | Done; sends only after your confirmation |
| Changesets and write-back | Gmail: `Talos/` labels; Microsoft 365: switched on per agreed use only |
| Argus, the service monitor | Running, with an outbound heartbeat |
| Talos Web | 127.0.0.1:7420 and over Tailscale, behind a password and an authenticator code |

## Contributing and security

How to work on Talos, and what is firm, is in [CONTRIBUTING.md](CONTRIBUTING.md). Report a vulnerability
privately, never in a public issue: [SECURITY.md](SECURITY.md).
