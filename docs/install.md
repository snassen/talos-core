# Installing Talos

Talos has two parts, and they live apart on purpose:

- **The code** (this repository): the same for everyone. Update it with `git pull`.
- **Your personal part**: who you are, your accounts, your rules and how your mail should be organised.
  It never goes into the code's repository. You keep it either **in a private git repository of your
  own**, or **only on your Mac**.

Secrets are a third thing: they live in the macOS Keychain and nowhere else, not in either part.

`talos where` shows, at any time, where each part is on your machine and why it lives there.

## 1. What you need

- A Mac with [Homebrew](https://brew.sh) and [uv](https://docs.astral.sh/uv/).
- PostgreSQL 18, on port **5433** (5432 is often taken by something else):

  ```bash
  brew install postgresql@18 pgvector
  brew services start postgresql@18      # then set port = 5433 in its postgresql.conf and restart
  ```

## 2. The code

```bash
git clone <this repository's URL> talos      # the green Code button on GitHub
cd talos
uv sync
```

## 3. Your personal part

Choose one of the two ways. Both hold the same files; only where they live differs.

### A. Only on this Mac (simplest)

```bash
uv run talos config init          # makes ~/TalosData/config from the examples; never overwrites a file
```

The folder is backed up with the rest of `~/TalosData` (Time Machine includes it unless you exclude it).

### B. In a private git repository of your own

Create an **empty private** repository on GitHub (or anywhere), for example `talos-personal`, then:

```bash
git clone https://github.com/<you>/talos-personal.git ~/TalosConfig
export TALOS_CONFIG=~/TalosConfig            # also in your shell profile
uv run talos config init                     # fills it from the examples
cd ~/TalosConfig && git add . && git commit -m "My Talos configuration" && git push
```

Keep it private: these files describe you. Nothing in them may be a password or a token; if you are about
to commit one, it belongs in the Keychain instead.

### What to edit

| File | What it is | Start from |
|---|---|---|
| `accounts.json` | the accounts Talos syncs (Gmail, Microsoft 365, IMAP, Teams), the Keychain item each needs, and every address that is "you" | `rules/accounts.example.json` |
| `recipient.txt` | who you are, in a few sentences: sent to Jev (the hosted model) with every question, so it knows whose mail it reads. Every word is paid for on every case | a generic text |
| `structure.json` | where every mail should go under `Talos/` in your mailboxes (planned only; nothing moves without your go) | `rules/structure.json` |
| `rules/*.json` | your rules as data | `rules/example-rules.json` |
| `discovery/` | drafts of your systems and setup for Tune › Systems (optional) | empty |

## 4. Secrets, in the Keychain

You type them yourself; Talos never asks for them in a chat or a file:

```bash
security add-generic-password -U -s talos -a gmail:you@gmail.com -w        # a Gmail app password
security add-generic-password -U -s talos -a typesafe-api-key -w           # Jev, if you use it
uv run talos auth graph work                                                # Microsoft 365: a device-code sign-in
```

The item names are the `secret` fields in your `accounts.json`.

## 5. The database, the first sync, the web

```bash
uv run talos setup                 # database, migrations, your accounts, the taxonomy
uv run talos sync gmail --limit 500
uv run talos sync                  # continues; resumes where it stopped
uv run talos web setup             # Talos Web's door: your password and an authenticator app
uv run talos serve                 # http://127.0.0.1:7420
```

## 6. Running in the background

```bash
uv run talos launchd          # prints the 5-minute sync job
uv run talos launchd --web    # the web server, kept alive
```

Nothing is installed for you: save each job under `~/Library/LaunchAgents/` and `launchctl load` it. If you
use `TALOS_CONFIG`, `TALOS_HOME` or `TALOS_DSN`, run `talos launchd` with them set: the printed job then
carries them, since launchd gives a job none of your shell's settings.

The sync job also makes the nightly backup of your own work (TALOS_HOME/backups, 14 days kept): the database
holds things the vault cannot rebuild. [backup.md](backup.md) says what is in it and how to restore.

## 7. Check

```bash
uv run talos where       # the code, your personal part (yours or the examples), the data, the database,
                         # the Keychain (by name, never read), the services and the backups
uv run talos status      # accounts, last syncs, secrets present
```

## Updating

- The code: `git pull && uv sync`, then restart the web job. New migrations apply with `talos setup`.
- Your personal part: edit the files; with way B, commit and push them to your private repository.
