# Argus: the service monitor

Argus watches the services that run on your Mac and tells you when one stops, falls behind or
reports a failure. This page covers the check-in contract, the status model, how the notices reach
you, and the lines that wire a script of your own to it.

- Code: `src/talos/argus.py`.
- Migration: `src/talos/sql/017_argus.sql`.
- Routes: in `src/talos/web/app.py`.
- Command: `talos argus`.
- Page: Operations › Argus, plus an Argus widget on the Overview.

## How it works

Services **check in**. A *push* service calls Argus when it finishes a run. Only the service
knows what its own success is, and each check-in says when the next one is due. Argus holds no
schedule for any service.

Services that cannot be changed are **probes**. Argus checks them on a timer and records each
result as a check-in of its own. There are four kinds of probe:

| probe | spec | failing when |
|---|---|---|
| `http` | `{"url": "http://127.0.0.1:8888/health", "timeout": 5}` | the answer is not 2xx, or there is no answer |
| `launchd` | `{"label": "com.example.gateway", "ok_status": [0], "require_running": true}` | the job is not loaded, its last exit status is not in `ok_status`, or it is not running when required |
| `disk` | `{"path": "/", "min_free_gb": 20}` | the free space is below the threshold |
| `file_growth` | `{"path": "~/Library/Logs/my-backup.launchd.log"}` | the file grew past its acknowledged size. It stays failing until `talos argus ack SLUG`. |

Every probe spec also takes `every`, in seconds. The defaults are 60 for `http`, `launchd` and
`file_growth`, and 300 for `disk`.

An HTTP probe may only address this Mac (127.0.0.1, localhost or ::1). That keeps the outbound
heartbeat as the only call Argus makes off the Mac.

### Status

| status | meaning |
|---|---|
| `unknown` | registered, but it has never checked in |
| `up` | the last check-in was ok and its deadline has not passed |
| `late` | the deadline plus one grace period has passed |
| `down` | the deadline plus twice the grace period has passed |
| `failing` | the last check-in was a failure (a `/fail`, or a failed probe). It stays failing until an ok check-in. |
| `paused` | deliberately silenced. It stays on the page and is never hidden. |

The deadline is `expected_next_at`, which is the time of the check-in plus its
`expected_next_within`. `grace_seconds` belongs to the service and is set by
`talos argus register --grace`.

For a script that checks in with `expected_next_within` 300 and has a grace of 300 s, the service goes
late 10 minutes after a check-in and down 15 minutes after. Grace absorbs the jitter of a run's length.

A probe checks in with `expected_next_within` set to its `every` plus one sweep (30 s). If the
timers stop, every probed service goes late and then down.

### Notices

The **sweep** runs every 30 s inside `talos serve`. It is the only thing that changes a service's
stored status.

Each change into `late`, `down` or `failing`, and each return to `up` from one of those, raises a
**macOS notification**. `terminal-notifier` sends it if it is installed, and `osascript` otherwise.
The title and text are passed as arguments and are never spliced into a script.

The same state for the same service is announced at most once an hour, so a flapping service does
not become noise.

A check-in or failure that arrives over HTTP is swept at once, so a `/fail` reaches you within a
second.

Argus never sends mail or Teams messages (Talos sends only a mail you compose and confirm). If the Mac or
Talos itself dies, the hosted dead-man's switch alerts you by its own email.

### Timers

The timers are one asyncio task inside `talos serve`, which launchd keeps alive as
`<prefix>.web` (owner.json's service_prefix). The task runs:

- the sweep, every 30 s;
- the probes, each on its own interval;
- the outbound heartbeat, every N minutes (default 5);
- pruning, hourly: check-ins older than 30 days are deleted, and `argus_daily` keeps their daily
  counts.

The timers are **off unless** `TALOS_HOME/argus.json` turns them on:

```json
{"enabled": true, "beat_every_minutes": 5, "notify": true}
```

The heartbeat comes from this same process. If `talos serve` dies, the beats stop and the hosted
switch notices. That is how Argus's own liveness is covered.

### The outbound heartbeat (the dead-man's switch)

Every N minutes Talos sends one GET, with no payload, to a hosted check such as healthchecks.io.

- The URL holds the check's opaque token. It lives only in the Keychain, as `argus-heartbeat-url`,
  and never in a file, the environment or a log.
- A failure is recorded by its kind only (`HTTP 503`, `could not connect`, `ReadTimeout`), never
  by the exception text, which could carry the URL.
- The URL must be https.

The page shows the heartbeat's last success, last failure, last error and failures in a row. It
shows **not set up** when no URL is stored, and so does `talos argus status`.

## The check-in contract

```
POST /argus/checkin/<slug>
  ok=true                     optional; true unless ok=false, which counts as a failure
  expected_next_within=300    required: seconds until the next check-in (1 to 604800)
  summary=backed up 6 folders optional; one line, shown on the page (at most 300 characters)

POST /argus/fail/<slug>
  reason=the destination is not mounted           one line, shown on the page and in the notice
  expected_next_within=300                        optional

GET /argus/status             everything the page shows, as JSON
```

The fields go in the query string, as a form body (curl's `--data-urlencode`) or as a JSON body.
The body is limited to 4 kB.

The answer is `{"slug", "ok", "status", "expected_next_at"}`. The error codes are:

- **401**: no token, or the wrong one.
- **503**: no token has been set up in the Keychain.
- **404**: the slug is unknown. The token is checked first, so the slugs cannot be discovered
  without it.
- **400**: a field is wrong.
- **413**: the body is over 4 kB.

A check-in writes one row and one daily counter, then sweeps that one service. It takes about 7 ms
(see "Speed" below).

### Security

Check-ins come from scripts over curl, so they cannot carry the `X-Talos` header that the page's
own POSTs use. They carry the **check-in token** instead:

- **One global token** for all services, as the Keychain item `argus-checkin-token`. A per-service
  token would protect against one of your own scripts forging another's check-in, which
  is not a threat worth a Keychain item per script.
- The token is **sent as `Authorization: Bearer`**. `?token=` also works, but the header keeps
  the token out of any URL that could be logged. It is never read from the body.
- The token is **compared in constant time**.
- The server **reads the token once**, in the thread pool, not on the event loop. After a wrong
  token it reads the Keychain again at most once a minute, so a rotated token is picked up
  without a restart, and a stream of wrong guesses cannot become a stream of Keychain reads.
- **Without a token every check-in is refused.** It fails closed.
- The token matters **even on localhost**. Any web page open in a browser on the Mac can send a
  simple form POST to `127.0.0.1:7420` without a CORS preflight. The token is what stops such a
  page from forging a check-in, or a `/fail`, to silence or trip Argus.
- The routes **stay behind `TrustedHostMiddleware`**, which allows localhost plus the tailnet
  names in `web.json`. That defeats DNS rebinding.
- **Under a tailnet name**, `TailnetIdentity` also requires an allowed Tailscale user. A device on
  the tailnet could check in only as you and only with the token.
- **Every field is cleaned.** Control characters are removed and the summary is cut to 300
  characters, so the page and the notifications only ever get one line of plain text.

The page's pause, resume and acknowledge buttons are ordinary UI writes (`POST
/api/argus/<slug>/pause|resume|ack`) and need `X-Talos`.

### Never break the caller

**A ping must never break the service that sends it.**

In shell, always use `curl -fsS -m 3 … >/dev/null 2>&1 || true`: give up after 3 seconds and
swallow any failure.

Inside Talos, `talos sync` checks in as `talos-sync` through `argus.ping()`, and its enrichment hook
as `talos-enrich` (talos.incremental). It opens its own
connection with a 3-second connect and statement timeout, catches everything and returns
True/False.

`tests/test_argus.py` holds this three ways:

- **ping() never raises.** It is tested against a bad DSN, an unknown slug, bad fields and any
  exception, including one from logging.
- **The sync survives a broken Argus.** The sync still completes when recording fails.
- **A scan.** Outside Argus itself, Talos reaches Argus only through `ping()`. Every curl line on
  this page, and every line `talos argus token` prints, has `-m 3` and ends with `|| true`.

## Setup

1. **Migrate.** This applies `017_argus.sql` and changes nothing else:

   ```bash
   uv run talos setup
   ```

2. **Register the day-one services.** This registers only the ones that are missing:

   ```bash
   uv run talos argus register
   ```

   The day-one services are:

   | slug | kind | watches |
   |---|---|---|
   | `talos-sync` | push | `talos sync`, which checks in at the end of every full run |
   | `talos-enrich` | push | `talos enrich jev new` run by `talos sync --then-rules` (when `enrich.json` turns it on), after each run; next within 15 min plus one sync |
   | `talos-backup` | push | the nightly backup of your own work (docs/backup.md); next within 26 hours |
   | `talos-web` | probe | launchd `<prefix>.web`, ok on last exit 0 or -15 (SIGTERM, launchd's own stop) |
   | `disk` | probe | free space on `/`, failing under 20 GB |

   Your own services join them from `argus-services.json` in your personal part (talos.personal): a
   list in the same shape as `talos.argus.DAY_ONE`, with a probe (http, launchd, disk, file_growth) or
   as a push service that checks in itself (see "Wiring a script of your own" below).

3. **Set the check-in token and the heartbeat URL** (human steps; the values never pass through
   an assistant). For the token, run `openssl rand -hex 32` and paste its output at the prompt of
   the second command:

   ```bash
   openssl rand -hex 32
   security add-generic-password -s talos -a argus-checkin-token -w
   ```

   For the heartbeat, paste the ping URL from healthchecks.io at the prompt:

   ```bash
   security add-generic-password -s talos -a argus-heartbeat-url -w
   ```

   On the provider's side, set the period to 5 minutes and the grace to about 10.

4. **Turn the timers on and restart the server:**

   ```bash
   echo '{"enabled": true, "beat_every_minutes": 5}' > ~/TalosData/argus.json
   launchctl kickstart -k gui/$(id -u)/local.talos.web   # your service_prefix, if not the default
   ```

5. **Check.** `talos argus beat` sends one heartbeat now:

   ```bash
   uv run talos argus beat
   uv run talos argus probe
   uv run talos argus status
   ```

macOS may ask once whether Talos web may read each new Keychain item. Choose Always Allow.

## Wiring a script of your own

A script you run yourself (a backup, a mirror, a cron job) checks in with a few lines of shell. The
example is a script registered as `my-backup` (`talos argus register my-backup --grace 300`, or listed
in your `argus-services.json`). `talos argus token my-backup` prints the same lines for any slug.

Create the token with `security` itself, as in setup step 3. The script's
`security find-generic-password` can then read it without a dialog, which would otherwise block a
background job.

Put these near the script's other helper functions:

```bash
# ---- Argus (Talos docs/argus.md): a ping can never break the script.
ARGUS="http://127.0.0.1:7420/argus"
ARGUS_TOKEN=$(security find-generic-password -s talos -a argus-checkin-token -w 2>/dev/null || true)
ARGUS_FAILED=""
argus_checkin() { # $1 seconds until the next run, $2 one-line summary
  curl -fsS -m 3 -X POST -H "Authorization: Bearer $ARGUS_TOKEN" --data-urlencode "expected_next_within=$1" --data-urlencode "summary=$2" "$ARGUS/checkin/my-backup" >/dev/null 2>&1 || true
}
argus_fail() { # $1 one-line reason
  ARGUS_FAILED=1
  curl -fsS -m 3 -X POST -H "Authorization: Bearer $ARGUS_TOKEN" --data-urlencode "reason=$1" "$ARGUS/fail/my-backup" >/dev/null 2>&1 || true
}
```

Report each way the script can fail, next to its own error handling:

```bash
argus_fail "the destination is not mounted"
```

At the end of a run, check in only if nothing failed. This keeps a failure on the page until the next
clean run. The deadline is when the next run is due, in seconds:

```bash
if [ -z "$ARGUS_FAILED" ]; then
  argus_checkin 3600 "backed up $count files"
fi
```

`argus_fail` sets `ARGUS_FAILED`, so it must be called in the main shell and not inside `$( … )`.

## Speed

A check-in over HTTP to the demo server (uvicorn, PostgreSQL over its Unix socket), measured with
curl's `time_total` over 50 calls, took 7 ms at the median, 24 ms at the 95th percentile and 50 ms
at most.

That is well inside curl's 3-second limit. The Keychain is read only on the first check-in.
