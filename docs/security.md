# Security

Talos holds all of your mail. The goal: no one can reach all of it in one easy go. This document says who can reach what, and what stops them.

## Threat model

| Who | Could they get the mail before? | Now |
|---|---|---|
| A device on your tailnet that isn't yours | No: the Tailscale identity check allows only your login | Also needs your password **and** an authenticator code |
| Your own devices, lost or borrowed | Yes: Talos Web had no sign-in | Needs your password and a code. A session ends after 24 h unused and after 7 days at most |
| A web page open in a browser on the Mac | Partly: the Host check and the X-Talos header stopped the obvious attacks | Also needs a session cookie, which is SameSite=Strict and HttpOnly |
| Another program on the Mac reaching 127.0.0.1:7420 | Yes: every API answered | 401 without a session |
| Another macOS user, or a system process, reaching the database | Yes: PostgreSQL trusted every local connection | Only your own macOS user, over the local socket (`peer`). TCP needs a password, and no role has one |
| Someone with a stolen session | Could download the whole archive | About 200 messages per 10 minutes. Then it stops until an authenticator code is entered, and a macOS notification says so |
| Someone logged in to the Mac as you | Yes | **Still yes.** The macOS login and FileVault are the boundary here: anything running as you can read your files. Keep the Mac's login strong and the screen locked |

## Signing in to Talos Web (`talos.webauth`, `talos.web.gate`)

- **Two factors.** Your password plus a 6-digit code from an authenticator app (TOTP, RFC 6238). A
  recovery code (8 were printed at setup) can stand in for the code, once each.
- **Where the credentials live.** In the Keychain, written only by `talos web setup` in your own Terminal:
  - the password as a scrypt hash (`web:password`),
  - the authenticator secret (`web:totp`),
  - the recovery-code hashes (`web:recovery`).

  The database holds none of them, so a copy of it cannot be used to sign in. A code works once: the last
  used time step is kept.
- **Sessions.** A random token in a cookie that is `HttpOnly` and `SameSite=Strict`, and `Secure` over
  Tailscale's https. The database keeps only its SHA-256. A session ends after 24 hours unused, after 7 days
  at most, on Sign out, or with *Sign out everywhere else* on Sources › Security (or
  `talos web signout-all`). Setting up again ends every session.
- **Wrong attempts.** Five failures within 15 minutes lock signing in for the rest of the 15 minutes, and
  the lock is announced as a macOS notification.
- **The bulk guard.** Opening a message, a conversation, a message's HTML or raw source, or an attachment
  counts as a read. A session gets 400 reads per 10 minutes (about 200 messages, since the pane loads the
  message and its HTML). Beyond that the reads stop with 429, the page asks for an authenticator code, and
  a notification says what happened. Lists and counts are not limited: they show snippets, not mail.
- **What passes without a session:**
  - the sign-in page and its two routes (`/login`, `/auth/login`, `/auth/status`);
  - `/static/` (code and icons, no data), and `/favicon.ico` (the tab icon, which browsers ask for on their own);
  - the Argus check-ins, which carry the check-in token.

  `/argus/status` takes either a session or the check-in token.
- **Headers on every response:**
  - `Content-Security-Policy` on pages: scripts only from `/static/` plus the hashed theme snippet, no
    plugins, no framing by others;
  - `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and `X-Frame-Options: SAMEORIGIN`;
  - `Cache-Control: no-store` on the API;
  - HSTS on the tailnet's https name.

  Messages' own HTML keeps its stricter sandboxed CSP.
- **Everything is logged** in `web_event`: sign-ins, failures, sign-outs, bulk stops, codes entered, and
  sessions made on the Mac. Sources › Security shows the latest.
- **The door can't be left open.** `web.app.create` enforces it always. The only exception is an app made
  for the test host name alone, where the older tests talk to the API directly. A test holds this
  (`test_the_door_cannot_be_left_open_outside_the_tests`).

## Commands

```bash
uv run talos web setup          # the password, the authenticator (QR in the Terminal) and recovery codes
uv run talos web sessions       # who is signed in, and the latest events at the door
uv run talos web signout-all    # end every session
uv run talos web session --minutes 30 --cookie-file /path/state.json
                                # a short session for a tool on the Mac (a headless page check). The cookie
                                # goes to a file only you can read, never to the screen; it is logged and
                                # announced
```

Lost phone: sign in with a recovery code, then run `talos web setup` again, which gives a new
authenticator secret and new recovery codes.

## The server end

- **PostgreSQL (port 5433):** listens on localhost only. `pg_hba.conf` lets in only your macOS user,
  over the local socket (`peer`). TCP needs a password (`scram-sha-256`), and no role has
  one. The data folder is private to you (700). Any other PostgreSQL server on 5432 is separate and untouched.
- **Talos Web** listens on 127.0.0.1 only. Tailscale's `serve` is the only way in from outside, and it
  passes only your tailnet identity (`web.json`).
- **The data folder** (TALOS_HOME, `~/TalosData` by default) is private to your account (700): the vault, logs and exports.
- **Secrets** are all in the Keychain.
- **The disk:** FileVault is on.

## Recommended, by hand (macOS settings Claude doesn't change)

1. **Turn the macOS firewall on**: System Settings › Network › Firewall. Talos needs no incoming
   connections: Tailscale brings them in.
2. **Check that the Time Machine backup is encrypted.** If the Mac backs up to a NAS or a disk, the backup
   holds the whole vault. Time Machine settings › the disk › *Encrypt backups*.
3. **Chrome's remote debugging port** (127.0.0.1:9222), if you open it, lets any program on the Mac drive that
   Chrome and its signed-in tabs, Talos included. Close it when it isn't in use.
4. **Tailscale:** in the admin console, keep device approval on, and consider an ACL that lets only your
   own devices reach the Mac on 8443.
5. **Screen Sharing / Remote Management** (ARD), if on: keep it limited to your account, and keep a strong
   Mac password.
