"""Argus, the hundred-eyed watchman: a monitor for the services that run on this Mac.

Services check in; Argus does not go looking. A `push` service calls

    POST /argus/checkin/<slug>   ok=true  expected_next_within=300  summary=6 repos in sync
    POST /argus/fail/<slug>      reason=sync parked: conflicts unresolved

and declares its own next deadline in every check-in, so Argus holds no schedule for anyone. A
`probe` service is one that cannot be changed (a third-party launchd agent, an HTTP port, the disk):
Argus checks it on a timer and records the result as a check-in of its own.

Status, computed from the facts of the last check-in and the time (status_of):

    unknown   registered, never checked in
    up        the last check-in was ok and its deadline has not passed
    late      past the deadline plus grace
    down      past the deadline plus twice the grace
    failing   the last check-in was a failure (/fail, or a probe that failed), until an ok one
    paused    deliberately silenced; shown, never hidden

Only the sweep (evaluate) changes the stored status, and each change can raise a macOS notification
(Notifier), throttled so the same state for the same service is announced at most once an hour.
Talos never sends mail or Teams messages; if the Mac or Talos itself dies, the hosted dead-man's
switch that the outbound heartbeat (beat) feeds alerts the owner by its own email.

The timers (sweep every 30 s, probes on their own intervals, the beat every N minutes, pruning)
run inside `talos serve` as one asyncio task (Monitor.run), when TALOS_HOME/argus.json says
{"enabled": true}. Tests drive Monitor.tick() directly, with a fixed clock.

In-process callers use ping(), which can never raise into its caller: a monitor that can break
the thing it watches is worse than none. Shell clients use `curl -fsS -m 3 … || true`.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from talos import personal

log = logging.getLogger("talos.argus")

SLUG = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")
STATUSES = ("unknown", "up", "late", "down", "failing", "paused")
BAD = ("late", "down", "failing")
TOKEN_KEY = "argus-checkin-token"
BEAT_KEY = "argus-heartbeat-url"

SWEEP_EVERY = 30              # seconds between ticks of the monitor
BEAT_ALERT_AFTER = 3           # outbound heartbeat failures in a row before a notification
NOTIFY_EVERY = 3600           # the same state of the same service is announced at most once an hour
KEEP_DAYS = 30                # check-ins older than this are pruned; the daily rollup stays
PRUNE_EVERY = 3600
BEAT_EVERY_MINUTES = 5
BEAT_TIMEOUT = 10.0
SYNC_EVERY = 300              # talos sync's own cadence (the 5-minute launchd job)
SUMMARY_MAX = 300
WITHIN_MAX = 7 * 86400        # a service may promise to be back within a week at most
GRACE_DEFAULT = 300

PROBE_TYPES = ("http", "launchd", "disk", "file_growth")
PROBE_EVERY = {"http": 60, "launchd": 60, "disk": 300, "file_growth": 60}


class ArgusError(ValueError):
    """A request Argus refuses; the message says why."""


class UnknownService(ArgusError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _local(t: datetime | None) -> str:
    return t.astimezone().strftime("%H:%M") if t else "—"


def clean_text(value: Any, limit: int = SUMMARY_MAX) -> str | None:
    """One line of text from a caller: control characters become spaces, then trimmed and cut."""
    if value is None:
        return None
    text = " ".join(re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).split())
    return text[:limit] or None


# ---------------------------------------------------------------- registry
def check_slug(slug: str) -> str:
    if not isinstance(slug, str) or not SLUG.fullmatch(slug):
        raise ArgusError("a slug is 1–63 characters of a-z, 0-9, '.', '_' and '-', starting with a letter or digit")
    return slug


def _loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_probe(spec: Any) -> dict:
    """A probe spec, checked and with its defaults filled in; ArgusError says what is wrong.

    http          {"url": "http://127.0.0.1:8888/health", "timeout": 5}; loopback only, so the
                  outbound heartbeat stays the one call that leaves the Mac
    launchd       {"label": "com.example.gateway", "ok_status": [0], "require_running": true}
    disk          {"path": "/", "min_free_gb": 20}
    file_growth   {"path": "~/Library/Logs/backup-script.log"}; growth is a failure until
                  acknowledged (talos argus ack)
    every         seconds between probes (defaults: http and launchd 60, disk 300, file_growth 60)
    """
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError:
            raise ArgusError("the probe is a JSON object") from None
    if not isinstance(spec, dict):
        raise ArgusError("the probe is a JSON object")
    kind = spec.get("type")
    if kind not in PROBE_TYPES:
        raise ArgusError(f"a probe's type is one of {', '.join(PROBE_TYPES)}")
    out: dict[str, Any] = {"type": kind}
    every = spec.get("every", PROBE_EVERY[kind])
    if isinstance(every, bool) or not isinstance(every, int) or not 10 <= every <= 86400:
        raise ArgusError("every is a whole number of seconds from 10 to 86400")
    out["every"] = every
    if kind == "http":
        url = spec.get("url")
        parts = urlsplit(url) if isinstance(url, str) else None
        if not parts or parts.scheme not in ("http", "https") or not _loopback(parts.hostname):
            raise ArgusError("an http probe's url is http(s) on this Mac only (127.0.0.1, localhost or ::1)")
        timeout = spec.get("timeout", 5)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
            raise ArgusError("timeout is seconds, more than 0 and at most 30")
        out.update(url=url, timeout=timeout)
    elif kind == "launchd":
        label = spec.get("label")
        if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", label):
            raise ArgusError("a launchd probe needs the job's label, e.g. com.example.gateway")
        ok_status = spec.get("ok_status", [0])
        if not isinstance(ok_status, list) or not ok_status or \
                not all(isinstance(x, int) and not isinstance(x, bool) for x in ok_status):
            raise ArgusError("ok_status is a list of whole numbers, the last exit statuses that count as fine")
        out.update(label=label, ok_status=ok_status, require_running=bool(spec.get("require_running", False)))
    elif kind == "disk":
        path = spec.get("path", "/")
        min_free = spec.get("min_free_gb", 20)
        if not isinstance(path, str) or not path:
            raise ArgusError("a disk probe's path is a folder on the volume, e.g. /")
        if isinstance(min_free, bool) or not isinstance(min_free, (int, float)) or min_free <= 0:
            raise ArgusError("min_free_gb is a number of gigabytes, more than 0")
        out.update(path=path, min_free_gb=min_free)
    elif kind == "file_growth":
        path = spec.get("path")
        if not isinstance(path, str) or not path:
            raise ArgusError("a file_growth probe needs the file's path")
        out.update(path=path)
    return out


def _service_fields(slug, name, kind, probe, grace_seconds, notes) -> dict:
    check_slug(slug)
    name = clean_text(name, 120) or slug
    if kind not in ("push", "probe"):
        raise ArgusError("kind is push or probe")
    if kind == "probe":
        probe = validate_probe(probe)
    elif probe is not None:
        raise ArgusError("a push service has no probe")
    if grace_seconds is None:
        grace_seconds = 2 * probe["every"] if probe else GRACE_DEFAULT
    if isinstance(grace_seconds, bool) or not isinstance(grace_seconds, int) or not 0 <= grace_seconds <= 604800:
        raise ArgusError("grace is a whole number of seconds from 0 to 604800")
    return dict(slug=slug, name=name, kind=kind, probe=Jsonb(probe) if probe else None, grace_seconds=grace_seconds,
                notes=clean_text(notes, 500))


def register(conn, slug: str, name: str | None = None, kind: str = "push", probe: dict | str | None = None,
             grace_seconds: int | None = None, notes: str | None = None) -> dict:
    """Add a service, or change one (its check-ins and status are kept)."""
    f = _service_fields(slug, name, kind, probe, grace_seconds, notes)
    row = conn.execute(
        "insert into argus_service (slug, name, kind, probe, grace_seconds, notes)"
        " values (%(slug)s, %(name)s, %(kind)s, %(probe)s, %(grace_seconds)s, %(notes)s)"
        " on conflict (slug) do update set name = excluded.name, kind = excluded.kind, probe = excluded.probe,"
        " grace_seconds = excluded.grace_seconds, notes = excluded.notes,"
        " probe_state = case when argus_service.probe is not distinct from excluded.probe"
        "                    then argus_service.probe_state else '{}' end"
        " returning *", f).fetchone()
    conn.commit()
    return row


# What Argus watches on day one: Talos's own services, and those the owner adds in
# TALOS_HOME/config/argus-services.json (talos.personal; a list of the same shape: their other services,
# such as a backup script or a local API). `talos argus register` with no slug adds those that are
# missing and leaves the ones already there as they are.
_PREFIX = personal.owner()["service_prefix"]
DAY_ONE = [
    dict(slug="talos-sync", name="Talos sync", kind="push", grace_seconds=600,
         notes=f"talos sync (launchd {_PREFIX}.sync, every 5 minutes) checks in, in-process, at the end of a"
               " full run; a failed account is a failing check-in"),
    dict(slug="talos-enrich", name="Talos enrichment", kind="push", grace_seconds=900,
         notes="talos sync --then-rules runs talos enrich jev new at most every 15 minutes when TALOS_HOME/enrich.json"
               " says {\"enabled\": true}; it checks in, in-process, after each run, expecting the next within the"
               " cadence plus one sync"),
    dict(slug="talos-backup", name="Talos backup", kind="push", grace_seconds=3600,
         notes="the nightly backup of the owner's own work (talos.backup): the first full sync after 02:00 makes it"
               " and checks in, expecting the next within 26 hours; `talos backup` makes one by hand"),
    dict(slug="talos-web", name="Talos web", kind="probe", grace_seconds=120,
         probe={"type": "launchd", "label": f"{_PREFIX}.web", "ok_status": [0, -15], "every": 60},
         notes="Argus runs inside this process, so this is its launchd record: the last exit status of the"
               " previous run (-15 is launchd's own stop, SIGTERM). If Talos web dies, the hosted switch notices."),
    dict(slug="disk", name="Free disk space", kind="probe", grace_seconds=600,
         probe={"type": "disk", "path": "/", "min_free_gb": 20, "every": 300},
         notes="Failing below 20 GB free on the startup volume"),
] + personal.data("argus-services.json", [])


def seed(conn) -> list[str]:
    """Register the day-one services that are not registered yet. Returns the slugs added."""
    added = []
    for d in DAY_ONE:
        f = _service_fields(d["slug"], d.get("name") or d["slug"], d["kind"], d.get("probe"), d.get("grace_seconds", 300),
                            d.get("notes", ""))
        if conn.execute(
                "insert into argus_service (slug, name, kind, probe, grace_seconds, notes)"
                " values (%(slug)s, %(name)s, %(kind)s, %(probe)s, %(grace_seconds)s, %(notes)s)"
                " on conflict (slug) do nothing returning slug", f).fetchone():
            added.append(d["slug"])
    conn.commit()
    return added


def set_paused(conn, slug: str, paused: bool, *, now: datetime | None = None) -> dict:
    now = now or utcnow()
    row = conn.execute("update argus_service set paused = %s, paused_at = case when %s then %s end"
                       " where slug = %s returning *", (paused, paused, now, check_slug(slug))).fetchone()
    if not row:
        conn.rollback()
        raise UnknownService(f"no service {slug!r}")
    conn.commit()
    return row


# ---------------------------------------------------------------- check-ins
def _within(value: Any, required: bool) -> int | None:
    if value in (None, ""):
        if required:
            raise ArgusError("expected_next_within is required: the seconds until the service's next check-in")
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ArgusError("expected_next_within is a whole number of seconds") from None
    if isinstance(value, bool) or not 1 <= n <= WITHIN_MAX:
        raise ArgusError(f"expected_next_within is from 1 to {WITHIN_MAX} seconds")
    return n


def record(conn, slug: str, *, ok: bool, expected_next_within: Any = None, summary: Any = None,
           source: str = "push", now: datetime | None = None, commit: bool = True) -> dict:
    """Store one check-in (ok) or failure (not ok) and the service's new facts; returns the service row.

    An ok check-in must say when the next one is due (expected_next_within); a failure may. The
    stored status is left to the sweep (evaluate), which is what notifies."""
    now = now or utcnow()
    within = _within(expected_next_within, required=ok)
    text = clean_text(summary)
    nxt = now + timedelta(seconds=within) if within else None
    row = conn.execute(
        "update argus_service set last_checkin_at = %(now)s, last_ok = %(ok)s,"
        " last_ok_at = case when %(ok)s then %(now)s else last_ok_at end,"
        " last_fail_at = case when %(ok)s then last_fail_at else %(now)s end,"
        " last_summary = %(summary)s, expected_next_at = coalesce(%(next)s, expected_next_at)"
        " where slug = %(slug)s returning *",
        dict(now=now, ok=ok, summary=text, next=nxt, slug=slug)).fetchone()
    if not row:
        conn.rollback()
        raise UnknownService(f"no service {slug!r}; register it first (talos argus register {slug} …)")
    conn.execute("insert into argus_checkin (slug, at, ok, summary, expected_next_at, source)"
                 " values (%s, %s, %s, %s, %s, %s)", (slug, now, ok, text, row["expected_next_at"], source))
    conn.execute("insert into argus_daily (slug, day, ok, fail) values (%s, %s, %s, %s)"
                 " on conflict (slug, day) do update set ok = argus_daily.ok + excluded.ok,"
                 " fail = argus_daily.fail + excluded.fail",
                 (slug, now.astimezone().date(), int(ok), int(not ok)))
    if commit:
        conn.commit()
    return row


def ping(dsn: str, slug: str, *, ok: bool = True, expected_next_within: Any = None, summary: Any = None,
         timeout: int = 3) -> bool:
    """A check-in from inside Talos (talos sync), which can never raise into its caller or hold it up.

    It opens its own short connection (never the caller's, whose transaction it must not touch), with
    a connect and statement timeout. Any failure is logged and swallowed; returns whether it was stored."""
    try:
        with psycopg.connect(dsn, connect_timeout=timeout, row_factory=dict_row,
                             options=f"-c statement_timeout={int(timeout * 1000)}") as c:
            record(c, check_slug(slug), ok=ok, expected_next_within=expected_next_within, summary=summary)
        return True
    except Exception as exc:  # noqa: BLE001 — the whole point: nothing gets out
        try:
            log.warning("argus check-in for %s not recorded: %s", slug, type(exc).__name__)
        except Exception:  # noqa: BLE001
            pass
        return False


# ---------------------------------------------------------------- status
def deadlines(svc: dict) -> tuple[datetime | None, datetime | None]:
    """(late_at, down_at): the deadline plus grace, and plus twice the grace."""
    nxt = svc.get("expected_next_at")
    if nxt is None:
        return None, None
    grace = timedelta(seconds=svc["grace_seconds"])
    return nxt + grace, nxt + 2 * grace


def status_of(svc: dict, now: datetime) -> str:
    if svc["paused"]:
        return "paused"
    if svc["last_checkin_at"] is None:
        return "unknown"
    if svc["last_ok"] is False:
        return "failing"
    late_at, down_at = deadlines(svc)
    if late_at is None or now < late_at:
        return "up"
    return "late" if now < down_at else "down"


def notice_text(svc: dict, status: str) -> tuple[str, str]:
    name = svc["name"]
    if status == "late":
        return f"Argus: {name} is late", f"Expected by {_local(svc['expected_next_at'])}; last check-in {_local(svc['last_checkin_at'])}."
    if status == "down":
        return f"Argus: {name} is down", f"No check-in since {_local(svc['last_checkin_at'])} (expected by {_local(svc['expected_next_at'])})."
    if status == "failing":
        return f"Argus: {name} is failing", svc.get("last_summary") or "It reported a failure."
    return f"Argus: {name} is up again", svc.get("last_summary") or "It checked in."


def evaluate(conn, *, now: datetime | None = None, notifier: Callable[[str, str], Any] | None = None,
             slugs: list[str] | None = None) -> list[dict]:
    """The sweep: bring each service's stored status up to date and announce the changes.

    A move into late, down or failing is announced, and so is the move back to up from one of them
    ('recovered'), each at most once per NOTIFY_EVERY seconds for the same service."""
    now = now or utcnow()
    rows = conn.execute("select * from argus_service" + (" where slug = any(%s)" if slugs else "")
                        + " order by slug for update", (slugs,) if slugs else None).fetchall()
    changes = []
    for svc in rows:
        new, old = status_of(svc, now), svc["status"]
        if new == old:
            continue
        key = new if new in BAD else "recovered" if new == "up" and old in BAD else None
        notified = dict(svc["notified"] or {})
        announced = False
        if key:
            last = notified.get(key)
            recent = last is not None and (now - datetime.fromisoformat(last)).total_seconds() < NOTIFY_EVERY
            if not recent and notifier is not None:
                title, text = notice_text(svc, new)
                try:
                    notifier(title, text)
                    announced = True
                except Exception:  # noqa: BLE001 — a notification that fails must not stop the sweep
                    log.exception("argus notification failed")
            if announced:
                notified[key] = now.isoformat()
        conn.execute("update argus_service set status = %s, status_since = %s, notified = %s where slug = %s",
                     (new, now, Jsonb(notified), svc["slug"]))
        changes.append({"slug": svc["slug"], "from": old, "to": new, "announced": announced})
    conn.commit()
    return changes


class Notifier:
    """A macOS notification, from the server process: terminal-notifier when it is installed,
    otherwise osascript. Title and text go in as arguments, never spliced into a script, so a
    summary a service sent cannot become AppleScript."""

    SCRIPT = ["-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
              "-e", "end run"]

    def __init__(self, runner: Callable[..., Any] = subprocess.Popen, which: Callable[[str], str | None] = shutil.which):
        self.runner, self.which = runner, which

    def __call__(self, title: str, text: str) -> None:
        title, text = clean_text(title, 120) or "Argus", clean_text(text, 240) or ""
        tn = self.which("terminal-notifier")
        if tn:
            args = [tn, "-title", title, "-message", text or " ", "-group", "talos-argus"]
        else:
            args = ["osascript", *self.SCRIPT, title, text]
        self.runner(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)


# ---------------------------------------------------------------- probes
@dataclass
class ProbeTools:
    """The system calls the probes make, replaceable in tests."""
    http: httpx.Client | None = None
    launchctl: Callable[[], str] | None = None
    disk_usage: Callable[[str], Any] = shutil.disk_usage
    file_size: Callable[[str], int | None] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.http is None:
            self.http = httpx.Client()
        if self.file_size is None:
            self.file_size = _file_size
        if self.launchctl is None:
            self.launchctl = _launchctl_list


def _file_size(path: str) -> int | None:
    try:
        return os.stat(Path(path).expanduser()).st_size
    except FileNotFoundError:
        return None


def _launchctl_list() -> str:
    return subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5, check=True).stdout


def parse_launchctl_list(text: str) -> dict[str, tuple[int | None, int | None]]:
    """`launchctl list` → {label: (pid or None, last exit status or None)}. A negative status is the
    signal that ended the last run (-15 SIGTERM, -9 SIGKILL)."""
    jobs = {}
    for line in text.splitlines():
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) < 3 or parts[0] == "PID":
            continue
        pid, status, label = parts[0].strip(), parts[1].strip(), parts[2].strip()
        try:
            jobs[label] = (int(pid) if pid not in ("-", "") else None, int(status) if status not in ("-", "") else None)
        except ValueError:
            continue
    return jobs


def probe_http(spec: dict, client: httpx.Client) -> tuple[bool, str]:
    started = time.monotonic()
    r = client.get(spec["url"], timeout=spec["timeout"])
    ms = round(1000 * (time.monotonic() - started))
    return 200 <= r.status_code < 300, f"HTTP {r.status_code} in {ms} ms"


def probe_launchd(spec: dict, jobs: dict[str, tuple[int | None, int | None]],
                  state: dict | None = None) -> tuple[bool, str, dict]:
    """A launchd job. For a job that is running now, launchd's "last exit status" belongs to the
    run *before* it, so a bad status only counts when the job restarted since the last probe (a new
    pid): that is a crash. A job that has kept the same pid since the last look is up, with the old
    status mentioned. A job that is not running is judged by its last exit status as before."""
    state = dict(state or {})
    if spec["label"] not in jobs:
        return False, f"{spec['label']} is not loaded in launchd", state
    pid, status = jobs[spec["label"]]
    bad = status is not None and status not in spec["ok_status"]
    new_state = {"pid": pid, "status": status}
    if not pid:
        said = f"not running, last exit status {status if status is not None else '—'}"
        return not (spec["require_running"] or bad), said, new_state
    restarted = state.get("pid") is not None and state["pid"] != pid
    if bad and restarted:
        return False, f"crashed (exit status {status}) and was restarted as pid {pid}", new_state
    said = f"running (pid {pid})" + (f"; the run before it ended with exit status {status}" if bad else "")
    return True, said, new_state


def probe_disk(spec: dict, disk_usage) -> tuple[bool, str]:
    free_gb = disk_usage(spec["path"]).free / 1e9
    ok = free_gb >= spec["min_free_gb"]
    return ok, f"{free_gb:.1f} GB free on {spec['path']}" + ("" if ok else f", under {spec['min_free_gb']} GB")


def probe_file_growth(spec: dict, state: dict, file_size) -> tuple[bool, str, dict]:
    """Growth past the acknowledged size is a failure, and stays one until acknowledged."""
    size = file_size(spec["path"])
    size = 0 if size is None else size
    if "baseline" not in state:
        return True, f"{size:,} bytes; watching for growth", {"baseline": size}
    baseline = state["baseline"]
    if size < baseline:  # truncated or rotated: the new size is the new baseline
        return True, f"{size:,} bytes (was {baseline:,}; truncated)", {"baseline": size}
    if size > baseline:
        return False, f"grew by {size - baseline:,} bytes to {size:,}: a crash outside its own error handling?", state
    return True, f"{size:,} bytes, unchanged", state


def run_probe(svc: dict, tools: ProbeTools, jobs: dict | None) -> tuple[bool, str, dict | None]:
    spec, state = svc["probe"], svc["probe_state"] or {}
    try:
        if spec["type"] == "http":
            return (*probe_http(spec, tools.http), None)
        if spec["type"] == "launchd":
            return probe_launchd(spec, jobs if jobs is not None else parse_launchctl_list(tools.launchctl()), state)
        if spec["type"] == "disk":
            return (*probe_disk(spec, tools.disk_usage), None)
        if spec["type"] == "file_growth":
            return probe_file_growth(spec, state, tools.file_size)
        return False, f"unknown probe type {spec['type']!r}", None
    except httpx.TimeoutException:
        return False, f"no answer within {spec.get('timeout')} s", None
    except httpx.ConnectError:
        return False, "connection refused: nothing is listening", None
    except Exception as exc:  # noqa: BLE001 — a broken probe is a failing service, not a broken monitor
        return False, f"the probe failed: {type(exc).__name__}", None


def probe(conn, tools: ProbeTools, *, now: datetime | None = None, due_only: bool = True,
          slugs: list[str] | None = None) -> list[dict]:
    """Run the probes that are due (or all of them) and record each result as a check-in."""
    now = now or utcnow()
    rows = conn.execute("select * from argus_service where kind = 'probe' and not paused"
                        + (" and slug = any(%s)" if slugs else "") + " order by slug",
                        (slugs,) if slugs else None).fetchall()
    conn.commit()
    due = [r for r in rows if not due_only or r["probe_at"] is None
           or (now - r["probe_at"]).total_seconds() >= r["probe"]["every"] - 1]
    jobs = None
    if any(r["probe"]["type"] == "launchd" for r in due):
        try:
            jobs = parse_launchctl_list(tools.launchctl())
        except Exception as exc:  # noqa: BLE001
            log.warning("launchctl list failed: %s", type(exc).__name__)
            jobs = None
    results = []
    for svc in due:
        if svc["probe"]["type"] == "launchd" and jobs is None:
            ok, said, state = False, "launchctl list failed", None
        else:
            ok, said, state = run_probe(svc, tools, jobs)
        # The next probe comes at the first tick after `every` seconds, so up to one sweep later.
        record(conn, svc["slug"], ok=ok, expected_next_within=svc["probe"]["every"] + SWEEP_EVERY, summary=said,
               source="probe", now=now, commit=False)
        conn.execute("update argus_service set probe_at = %s" + (", probe_state = %s" if state is not None else "")
                     + " where slug = %s", (now, Jsonb(state), svc["slug"]) if state is not None else (now, svc["slug"]))
        conn.commit()
        results.append({"slug": svc["slug"], "ok": ok, "summary": said})
    return results


def acknowledge(conn, slug: str, tools: ProbeTools | None = None, *, now: datetime | None = None) -> dict:
    """A file_growth probe: accept the file's current size as the new normal, and check in ok."""
    now = now or utcnow()
    tools = tools or ProbeTools()
    svc = conn.execute("select * from argus_service where slug = %s", (check_slug(slug),)).fetchone()
    if not svc:
        raise UnknownService(f"no service {slug!r}")
    if not svc["probe"] or svc["probe"]["type"] != "file_growth":
        raise ArgusError("only a file_growth probe is acknowledged; the others recover on their own")
    size = tools.file_size(svc["probe"]["path"]) or 0
    conn.execute("update argus_service set probe_state = %s, probe_at = %s where slug = %s",
                 (Jsonb({"baseline": size}), now, slug))
    return record(conn, slug, ok=True, expected_next_within=svc["probe"]["every"] + SWEEP_EVERY,
                  summary=f"{size:,} bytes, acknowledged", source="probe", now=now)


# ---------------------------------------------------------------- the outbound heartbeat
def beat_url_problem(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return "the heartbeat URL must be https (it carries the check's token)"
    return None


def beat(conn, url: str | None, client: httpx.Client, *, now: datetime | None = None) -> dict:
    """One GET to the hosted dead-man's switch: no payload, no headers of ours. The URL holds the
    check's opaque token, so it is never logged or stored; an error is recorded by its kind only."""
    now = now or utcnow()
    if not url:
        return {"sent": False, "state": "not set up"}
    error = beat_url_problem(url)
    if not error:
        try:
            r = client.get(url, timeout=BEAT_TIMEOUT)
            error = None if 200 <= r.status_code < 300 else f"HTTP {r.status_code}"
        except httpx.TimeoutException:
            error = f"no answer within {BEAT_TIMEOUT:g} s"
        except httpx.ConnectError:
            error = "could not connect"
        except Exception as exc:  # noqa: BLE001 — str(exc) could carry the URL; only its kind is kept
            error = type(exc).__name__
    if error is None:
        conn.execute("update argus_beat set last_attempt_at = %s, last_ok_at = %s, failures = 0,"
                     " total_ok = total_ok + 1 where id = 1", (now, now))
    else:
        conn.execute("update argus_beat set last_attempt_at = %s, last_fail_at = %s, last_error = %s,"
                     " failures = failures + 1, total_failures = total_failures + 1 where id = 1", (now, now, error))
    conn.commit()
    return {"sent": error is None, "state": "ok" if error is None else "failing", "error": error, "at": now}


def beat_state(conn, configured: bool) -> dict:
    row = conn.execute("select * from argus_beat where id = 1").fetchone() or {}
    conn.commit()
    if not configured:
        state = "not set up"
    elif not row.get("last_attempt_at"):
        state = "not sent yet"
    elif row.get("failures"):
        state = "failing"
    else:
        state = "ok"
    return {"configured": configured, "state": state, **{k: row.get(k) for k in (
        "last_attempt_at", "last_ok_at", "last_fail_at", "last_error", "failures", "total_ok", "total_failures")}}


# ---------------------------------------------------------------- pruning
def prune(conn, *, now: datetime | None = None, keep_days: int = KEEP_DAYS) -> int:
    """Delete check-ins older than keep_days; the daily rollup keeps their counts."""
    now = now or utcnow()
    n = conn.execute("delete from argus_checkin where at < %s", (now - timedelta(days=keep_days),)).rowcount
    conn.commit()
    return n


# ---------------------------------------------------------------- the page's data
def overview(conn, *, now: datetime | None = None, beat_configured: bool = False, days: int = 14) -> dict:
    now = now or utcnow()
    rows = conn.execute("select * from argus_service order by slug").fetchall()
    since = (now.astimezone() - timedelta(days=days - 1)).date()
    daily: dict[str, dict[date, dict]] = {}
    for d in conn.execute("select slug, day, ok, fail from argus_daily where day >= %s", (since,)):
        daily.setdefault(d["slug"], {})[d["day"]] = {"ok": d["ok"], "fail": d["fail"]}
    fails: dict[str, list] = {}
    for f in conn.execute(
            "select slug, at, summary from (select slug, at, summary, row_number() over (partition by slug order by at desc) n"
            " from argus_checkin where not ok) x where n <= 3 order by slug, at desc"):
        fails.setdefault(f["slug"], []).append({"at": f["at"], "summary": f["summary"]})
    services = []
    for svc in rows:
        late_at, down_at = deadlines(svc)
        per_day = daily.get(svc["slug"], {})
        services.append({
            **{k: svc[k] for k in ("slug", "name", "kind", "probe", "grace_seconds", "paused", "paused_at", "notes",
                                   "last_checkin_at", "last_ok_at", "last_fail_at", "last_ok", "last_summary",
                                   "expected_next_at", "status_since")},
            "status": status_of(svc, now), "late_at": late_at, "down_at": down_at,
            "history": [{"day": (since + timedelta(days=i)).isoformat(),
                         **per_day.get(since + timedelta(days=i), {"ok": 0, "fail": 0})} for i in range(days)],
            "recent_failures": fails.get(svc["slug"], []),
        })
    counts = {st: sum(1 for s in services if s["status"] == st) for st in STATUSES}
    conn.commit()
    return {"now": now, "services": services, "counts": counts,
            "all_up": bool(services) and all(s["status"] == "up" for s in services),
            "beat": beat_state(conn, beat_configured)}


# ---------------------------------------------------------------- the check-in token
class TokenCheck:
    """The check-in token (Keychain item argus-checkin-token), read once and kept in memory.

    With no token set up every check-in is refused (fail closed). A wrong token makes it look
    again at most once a minute, so a rotated token is picked up without a restart and a stream
    of wrong guesses cannot turn into a stream of Keychain reads."""

    def __init__(self, reader: Callable[[], str | None], recheck: float = 60.0, clock=time.monotonic):
        self.reader, self.recheck, self.clock = reader, recheck, clock
        self.token: str | None = None
        self.read_at: float | None = None

    def _load(self) -> None:
        self.read_at = self.clock()
        try:
            self.token = self.reader() or None
        except Exception:  # noqa: BLE001
            log.warning("could not read the check-in token from the Keychain")
            self.token = None

    def configured(self) -> bool:
        if self.read_at is None or (self.token is None and self.clock() - self.read_at >= self.recheck):
            self._load()
        return self.token is not None

    def __call__(self, presented: str | None) -> bool:
        if self.read_at is None:
            self._load()
        if self.token and presented and hmac.compare_digest(presented.encode(), self.token.encode()):
            return True
        if self.clock() - self.read_at >= self.recheck:
            self._load()
            return bool(self.token and presented and hmac.compare_digest(presented.encode(), self.token.encode()))
        return False


# ---------------------------------------------------------------- the timers
def load_settings(home: Path) -> dict:
    """TALOS_HOME/argus.json: {"enabled": true, "beat_every_minutes": 5, "notify": true}. Absent: off."""
    path = home / "argus.json"
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        log.warning("argus.json is not valid JSON; the Argus timers stay off")
        return {"enabled": False}
    return data if isinstance(data, dict) else {"enabled": False}


@dataclass
class Monitor:
    """The timers: sweep, due probes, the outbound beat and pruning, in one tick."""
    dsn: str
    notifier: Callable[[str, str], Any] | None = None
    tools: ProbeTools = field(default_factory=ProbeTools)
    beat_url: Callable[[], str | None] = lambda: None
    beat_client: httpx.Client | None = None
    beat_every: int = BEAT_EVERY_MINUTES * 60
    last_tick_at: datetime | None = None
    last_beat_try: datetime | None = None
    last_prune_at: datetime | None = None
    beat_announced: str | None = None       # 'failing' or 'recovered', so each is said once per change
    ticks: int = 0

    def tick(self, now: datetime | None = None) -> dict:
        now = now or utcnow()
        out: dict[str, Any] = {}
        with psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=5) as conn:
            for step, fn in (("probes", lambda: probe(conn, self.tools, now=now)),
                             ("changes", lambda: evaluate(conn, now=now, notifier=self.notifier)),
                             ("beat", lambda: self._beat(conn, now)),
                             ("pruned", lambda: self._prune(conn, now))):
                try:
                    out[step] = fn()
                except Exception:  # noqa: BLE001 — one broken step must not stop the others, or the loop
                    conn.rollback()
                    log.exception("argus %s failed", step)
                    out[step] = "error"
        self.last_tick_at, self.ticks = now, self.ticks + 1
        return out

    def _beat(self, conn, now):
        row = conn.execute("select last_attempt_at from argus_beat where id = 1").fetchone()
        conn.commit()
        last = max(filter(None, [row and row["last_attempt_at"], self.last_beat_try]), default=None)
        if last is not None and (now - last).total_seconds() < self.beat_every - 1:
            return None
        self.last_beat_try = now
        url = self.beat_url()
        if not url:
            return {"sent": False, "state": "not set up"}
        if self.beat_client is not None:
            result = beat(conn, url, self.beat_client, now=now)
        else:
            with httpx.Client() as client:
                result = beat(conn, url, client, now=now)
        self._announce_beat(conn, result)
        return result

    def _announce_beat(self, conn, result: dict) -> None:
        """The dead-man's switch not reaching its host is itself worth a notification: after
        BEAT_ALERT_AFTER failures in a row once, and once again when it recovers."""
        if self.notifier is None:
            return
        failures = conn.execute("select failures from argus_beat where id = 1").fetchone()["failures"]
        conn.commit()
        key = None
        if result.get("state") == "failing" and failures >= BEAT_ALERT_AFTER and self.beat_announced != "failing":
            key, text = "failing", (f"The outbound heartbeat has failed {failures} times in a row"
                                    f" ({result.get('error')}); the hosted switch will raise its own alarm.")
        elif result.get("state") == "ok" and self.beat_announced == "failing":
            key, text = "recovered", "The outbound heartbeat reaches its host again."
        if key:
            try:
                self.notifier("Argus: heartbeat " + ("failing" if key == "failing" else "back"), text)
                self.beat_announced = key
            except Exception:  # noqa: BLE001 — a notification that fails must not stop the timers
                log.exception("argus heartbeat notification failed")

    def _prune(self, conn, now):
        if self.last_prune_at and (now - self.last_prune_at).total_seconds() < PRUNE_EVERY:
            return None
        self.last_prune_at = now
        return prune(conn, now=now)

    async def run(self, stop: asyncio.Event) -> None:
        log.info("argus: timers started (sweep every %s s, beat every %s s)", SWEEP_EVERY, self.beat_every)
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.tick)
            except Exception:  # noqa: BLE001 — the loop outlives any one tick
                log.exception("argus tick failed")
            try:
                await asyncio.wait_for(stop.wait(), SWEEP_EVERY)
            except TimeoutError:
                pass
