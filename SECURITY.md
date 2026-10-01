# Security policy

Talos holds people's entire mail archive, so security reports are taken seriously and handled
before anything else.

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately to the maintainer through GitHub's
private vulnerability reporting: on the repository, go to *Security › Report a vulnerability*.

Include what you found, how to reproduce it, and what an attacker could reach. You will get an
answer as soon as the maintainer can, and credit in the fix if you want it. Please give a reasonable
time to fix it before you disclose it.

Never include real mail or anyone's personal data in a report. A reproduction with invented data is
always enough.

## In scope

- Anything that lets someone read mail without signing in to Talos Web, or beyond the read budget
  of a session.
- Bypassing the sign-in (password and authenticator code), sessions, the host check, the Tailscale
  identity check or the check-in token.
- Anything that makes Talos send mail without the owner's confirmation, delete permanently, write to
  a mail server outside a committed changeset, or alter an original in the vault. These break
  Talos's promises ([CLAUDE.md](CLAUDE.md)).
- Script injection through mail content (HTML, headers, attachments, Teams messages) into Talos Web.
- Secrets reaching files, logs, environment variables or anything that leaves the Mac.
- More leaving the Mac than documented (for example in the enrichment case records).

## Out of scope

- Someone who is already logged in to the Mac as the owner. The macOS login and FileVault are the
  boundary there ([docs/security.md](docs/security.md)).
- Weaknesses in the mail providers, Tailscale, PostgreSQL or macOS themselves (report those to their
  makers), unless Talos uses them unsafely.
- An instance deliberately configured against the documentation.

## How Talos is protected

[docs/security.md](docs/security.md) describes the threat model, the sign-in, sessions, the bulk
guard, the headers, what leaves the Mac, and the hardening recommended on the Mac itself.
