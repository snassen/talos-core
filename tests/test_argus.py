"""Argus, the service monitor: check-ins, status over time, probes, the outbound beat, notices.

Every clock is fixed and every system call is replaced: no test reads the Keychain, runs
launchctl or osascript, or makes a network call (the beat and the HTTP probe go to MockTransport).
"""

import argparse
import importlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from talos import argus, cli, db
from talos.config import Settings
from talos.web import app

T0 = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)
TOKEN = "0123456789abcdef-test-token"
ROOT = Path(__file__).parent.parent


@pytest.fixture
def ac(database):
    """A connection with Argus's tables empty and the beat's row reset."""
    with db.connect(database) as c:
        c.execute("truncate argus_checkin, argus_daily, argus_service cascade")
        c.execute("update argus_beat set last_attempt_at = null, last_ok_at = null, last_fail_at = null,"
                  " last_error = null, failures = 0, total_ok = 0, total_failures = 0")
        c.commit()
        yield c
        c.rollback()


class Notes(list):
    def __call__(self, title, text):
        self.append((title, text))


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def status(c, slug):
    return c.execute("select status from argus_service where slug = %s", (slug,)).fetchone()["status"]


def client(database, tmp_path, token=TOKEN, notes=None):
    return TestClient(app.create(Settings(home=tmp_path, dsn=database), allowed_hosts=["testserver"],
                                 argus_token=lambda: token, argus_notifier=notes if notes is not None else Notes(),
                                 beat_configured=lambda: False, argus_timers=False))


# ---------------------------------------------------------------- status over time
def test_a_service_is_unknown_until_it_checks_in_then_up_late_and_down_as_its_deadline_passes(ac):
    argus.register(ac, "job", "Job", grace_seconds=60)
    notes = Notes()
    assert argus.evaluate(ac, now=T0, notifier=notes) == []
    assert status(ac, "job") == "unknown"
    argus.record(ac, "job", ok=True, expected_next_within=300, summary="fine", now=T0)
    seen = {}
    for s in (1, 299, 300, 359, 360, 419, 420, 5000):
        argus.evaluate(ac, now=at(s), notifier=notes)
        seen[s] = status(ac, "job")
    assert seen == {1: "up", 299: "up", 300: "up", 359: "up", 360: "late", 419: "late", 420: "down", 5000: "down"}
    assert [t for t, _ in notes] == ["Argus: Job is late", "Argus: Job is down"]


def test_the_grace_decides_when_a_missed_deadline_counts_as_late(ac):
    argus.register(ac, "tight", grace_seconds=0)
    argus.register(ac, "loose", grace_seconds=600)
    for slug in ("tight", "loose"):
        argus.record(ac, slug, ok=True, expected_next_within=300, now=T0)
    svc = {r["slug"]: r for r in ac.execute("select * from argus_service")}
    assert argus.status_of(svc["tight"], at(300)) == "down"  # no grace: straight to down
    assert argus.status_of(svc["loose"], at(300)) == "up"
    assert argus.status_of(svc["loose"], at(899)) == "up"
    assert argus.status_of(svc["loose"], at(900)) == "late"
    assert argus.status_of(svc["loose"], at(1500)) == "down"


def test_each_check_in_carries_its_own_next_deadline(ac):
    argus.register(ac, "vs", grace_seconds=60)
    argus.record(ac, "vs", ok=True, expected_next_within=300, now=T0)          # active window: 5 minutes
    argus.record(ac, "vs", ok=True, expected_next_within=3600, now=at(300))    # night: hourly
    svc = ac.execute("select * from argus_service").fetchone()
    assert svc["expected_next_at"] == at(3900)
    assert argus.status_of(svc, at(3000)) == "up"


def test_an_ok_check_in_must_say_when_the_next_one_is_due(ac):
    argus.register(ac, "job")
    with pytest.raises(argus.ArgusError, match="expected_next_within"):
        argus.record(ac, "job", ok=True, now=T0)
    for bad in (0, -5, "soon", argus.WITHIN_MAX + 1):
        with pytest.raises(argus.ArgusError):
            argus.record(ac, "job", ok=True, expected_next_within=bad, now=T0)


def test_a_failure_is_failing_at_once_and_an_ok_check_in_recovers_it(ac):
    argus.register(ac, "vs", "backup-script")
    notes = Notes()
    argus.record(ac, "vs", ok=True, expected_next_within=300, now=T0)
    argus.evaluate(ac, now=T0, notifier=notes)
    argus.record(ac, "vs", ok=False, summary="cowork parked: conflicts unresolved", now=at(60))
    argus.evaluate(ac, now=at(60), notifier=notes)
    assert status(ac, "vs") == "failing"
    argus.evaluate(ac, now=at(5000), notifier=notes)  # a failure stays a failure, not merely down
    assert status(ac, "vs") == "failing"
    argus.record(ac, "vs", ok=True, expected_next_within=300, summary="6 repos in sync", now=at(5100))
    argus.evaluate(ac, now=at(5100), notifier=notes)
    assert status(ac, "vs") == "up"
    assert notes == [("Argus: backup-script is failing", "cowork parked: conflicts unresolved"),
                     ("Argus: backup-script is up again", "6 repos in sync")]
    row = ac.execute("select * from argus_service").fetchone()
    assert row["last_fail_at"] == at(60) and row["last_ok_at"] == at(5100)


def test_a_paused_service_shows_as_paused_and_is_never_announced(ac):
    argus.register(ac, "job", grace_seconds=60)
    notes = Notes()
    argus.record(ac, "job", ok=True, expected_next_within=300, now=T0)
    argus.set_paused(ac, "job", True, now=at(10))
    argus.evaluate(ac, now=at(10_000), notifier=notes)
    assert status(ac, "job") == "paused" and notes == []
    argus.record(ac, "job", ok=False, summary="broken", now=at(10_010))
    argus.evaluate(ac, now=at(10_010), notifier=notes)
    assert status(ac, "job") == "paused" and notes == []
    argus.set_paused(ac, "job", False)
    argus.evaluate(ac, now=at(10_020), notifier=notes)
    assert status(ac, "job") == "failing" and len(notes) == 1


def test_a_check_in_for_an_unknown_service_is_refused(ac):
    with pytest.raises(argus.UnknownService):
        argus.record(ac, "nobody", ok=True, expected_next_within=60, now=T0)
    assert ac.execute("select count(*) n from argus_checkin").fetchone()["n"] == 0


def test_the_day_one_services_are_registered_once_and_left_alone_after(ac):
    # Talos's own services; the tests have no argus-services.json, so nothing of the owner's is added
    assert argus.seed(ac) == ["talos-sync", "talos-enrich", "talos-backup", "talos-web", "disk"]
    ac.execute("update argus_service set grace_seconds = 999 where slug = 'talos-backup'")
    ac.commit()
    assert argus.seed(ac) == []
    assert ac.execute("select grace_seconds from argus_service where slug = 'talos-backup'").fetchone()["grace_seconds"] == 999
    probes = {r["slug"]: r["probe"] for r in ac.execute("select slug, probe from argus_service where kind = 'probe'")}
    assert probes["talos-web"]["label"] == "local.talos.web" and probes["disk"]["path"] == "/"


@pytest.fixture
def own_services(tmp_path, monkeypatch):
    """argus.DAY_ONE made again from a personal folder of its own; put back as it was afterwards."""
    monkeypatch.setenv("TALOS_CONFIG", str(tmp_path))
    yield tmp_path
    monkeypatch.delenv("TALOS_CONFIG")
    importlib.reload(argus)


def test_the_owners_own_services_in_argus_services_json_are_watched_after_talos_own(ac, own_services):
    (own_services / "argus-services.json").write_text(json.dumps([
        {"slug": "backup-script", "name": "Backup script", "kind": "push", "grace_seconds": 7200,
         "notes": "checks in after each run"},
        {"slug": "example-api", "name": "Example API", "kind": "probe",
         "probe": {"type": "http", "url": "http://127.0.0.1:8888/health", "every": 60}, "grace_seconds": 120,
         "notes": "a local API"}]), encoding="utf-8")
    importlib.reload(argus)
    assert argus.seed(ac) == ["talos-sync", "talos-enrich", "talos-backup", "talos-web", "disk",
                              "backup-script", "example-api"]
    probes = {r["slug"]: r["probe"] for r in ac.execute("select slug, probe from argus_service where kind = 'probe'")}
    assert probes["example-api"]["url"] == "http://127.0.0.1:8888/health"


def test_a_registration_is_checked(ac):
    for kwargs, msg in [(dict(slug="Bad Slug"), "slug"),
                        (dict(slug="x", kind="probe", probe={"type": "http", "url": "https://example.com/health"}), "this Mac"),
                        (dict(slug="x", kind="probe", probe={"type": "ftp"}), "type"),
                        (dict(slug="x", kind="probe", probe={"type": "launchd"}), "label"),
                        (dict(slug="x", kind="push", probe={"type": "disk"}), "no probe"),
                        (dict(slug="x", grace_seconds=-1), "grace")]:
        with pytest.raises(argus.ArgusError, match=msg):
            argus.register(ac, **kwargs)


# ---------------------------------------------------------------- notices
def test_a_notice_goes_to_osascript_as_arguments_never_as_script():
    calls = []
    n = argus.Notifier(runner=lambda args, **kw: calls.append(args), which=lambda name: None)
    n("Argus: x is failing", 'reason "with" quotes\nand a newline; end tell')
    args = calls[0]
    assert args[0] == "osascript" and args[-2:] == ["Argus: x is failing", 'reason "with" quotes and a newline; end tell']
    assert all("quotes" not in a for a in args[:-1])  # the text is never part of the script
    calls.clear()
    argus.Notifier(runner=lambda args, **kw: calls.append(args), which=lambda name: "/opt/homebrew/bin/terminal-notifier")("T", "M")
    assert calls[0][:5] == ["/opt/homebrew/bin/terminal-notifier", "-title", "T", "-message", "M"]


def test_the_same_state_is_announced_at_most_once_an_hour(ac):
    argus.register(ac, "flappy", "Flappy", grace_seconds=0)
    calls = []
    notifier = argus.Notifier(runner=lambda args, **kw: calls.append(args[-2]), which=lambda name: None)

    def flap(t):  # down at t, back up at t + 60
        argus.evaluate(ac, now=at(t), notifier=notifier)
        argus.record(ac, "flappy", ok=True, expected_next_within=30, now=at(t + 60))
        argus.evaluate(ac, now=at(t + 60), notifier=notifier)

    argus.record(ac, "flappy", ok=True, expected_next_within=30, now=T0)
    argus.evaluate(ac, now=T0, notifier=notifier)
    for t in (100, 1000, 2000, 3000):  # four flaps inside the hour
        flap(t)
    assert calls == ["Argus: Flappy is down", "Argus: Flappy is up again"]
    flap(3800)  # more than an hour after the first notices
    assert calls[2:] == ["Argus: Flappy is down", "Argus: Flappy is up again"]
    assert status(ac, "flappy") == "up"


def test_a_notification_that_fails_does_not_stop_the_sweep(ac):
    argus.register(ac, "a", grace_seconds=0)
    argus.register(ac, "b", grace_seconds=0)
    for s in ("a", "b"):
        argus.record(ac, s, ok=True, expected_next_within=10, now=T0)

    def broken(title, text):
        raise OSError("no osascript")
    changes = argus.evaluate(ac, now=at(100), notifier=broken)
    assert [c["to"] for c in changes] == ["down", "down"] and not any(c["announced"] for c in changes)


# ---------------------------------------------------------------- probes
LAUNCHCTL = ("PID\tStatus\tLabel\n"
             "51577\t0\tcom.example.ollama\n"
             "88550\t-15\tlocal.talos.web\n"
             "55458\t1\tcom.example.gateway\n"
             "-\t0\tcom.example.backup-script\n"
             "-\t-9\tcom.example.killed\n")


def test_launchctl_list_is_read_as_pid_and_last_exit_status():
    jobs = argus.parse_launchctl_list(LAUNCHCTL)
    assert jobs["local.talos.web"] == (88550, -15)
    assert jobs["com.example.backup-script"] == (None, 0)
    assert jobs["com.example.killed"] == (None, -9)
    assert "Label" not in jobs


def test_a_launchd_probe_judges_a_running_job_by_restarts_not_by_the_run_before_it():
    jobs = argus.parse_launchctl_list(LAUNCHCTL)
    spec = lambda **kw: argus.validate_probe({"type": "launchd", **kw})
    # running: the last exit status belongs to the run before, so it is up (the old status mentioned)
    ok, said, state = argus.probe_launchd(spec(label="com.example.gateway", require_running=True), jobs)
    assert ok and said == "running (pid 55458); the run before it ended with exit status 1"
    assert state == {"pid": 55458, "status": 1}
    # the same pid on the next look: still up
    assert argus.probe_launchd(spec(label="com.example.gateway", require_running=True), jobs, state)[0]
    # a new pid with a bad status since the last look: it crashed and launchd restarted it
    restarted = {**jobs, "com.example.gateway": (60001, 1)}
    ok, said, _ = argus.probe_launchd(spec(label="com.example.gateway", require_running=True), restarted, state)
    assert not ok and said == "crashed (exit status 1) and was restarted as pid 60001"
    # a clean restart is fine
    clean = {**jobs, "com.example.gateway": (60002, 0)}
    assert argus.probe_launchd(spec(label="com.example.gateway", require_running=True), clean, state)[0]
    # not running: judged by the last exit status and require_running, as before
    assert argus.probe_launchd(spec(label="local.talos.web", ok_status=[0, -15]), jobs)[0]
    assert argus.probe_launchd(spec(label="com.example.backup-script"), jobs)[0]
    assert not argus.probe_launchd(spec(label="com.example.backup-script", require_running=True), jobs)[0]
    assert not argus.probe_launchd(spec(label="com.example.killed"), jobs)[0]
    assert argus.probe_launchd(spec(label="gone.job"), jobs)[:2] == (False, "gone.job is not loaded in launchd")


def test_an_http_probe_is_ok_on_2xx_and_failing_otherwise():
    def handler(request):
        return {"/health": httpx.Response(200, text="ok"), "/sick": httpx.Response(503)}.get(
            request.url.path, httpx.Response(404))
    http = httpx.Client(transport=httpx.MockTransport(handler))
    spec = lambda path: argus.validate_probe({"type": "http", "url": f"http://127.0.0.1:8888{path}"})
    svc = lambda path: {"probe": spec(path), "probe_state": {}}
    assert argus.run_probe(svc("/health"), argus.ProbeTools(http=http), None)[:1] == (True,)
    ok, said, _ = argus.run_probe(svc("/sick"), argus.ProbeTools(http=http), None)
    assert not ok and said.startswith("HTTP 503")

    def refused(request):
        raise httpx.ConnectError("refused")
    ok, said, _ = argus.run_probe(svc("/health"), argus.ProbeTools(http=httpx.Client(transport=httpx.MockTransport(refused))), None)
    assert not ok and "nothing is listening" in said


def test_an_http_probe_may_only_look_at_this_mac():
    for url in ("http://127.0.0.1:8888/health", "http://localhost/x", "http://[::1]:80/"):
        argus.validate_probe({"type": "http", "url": url})
    for url in ("https://healthchecks.io/", "http://192.168.1.2/", "http://127.0.0.1.evil.com/", "file:///etc/passwd"):
        with pytest.raises(argus.ArgusError):
            argus.validate_probe({"type": "http", "url": url})


def test_a_disk_probe_fails_under_its_threshold():
    spec = argus.validate_probe({"type": "disk", "path": "/", "min_free_gb": 20})
    assert argus.probe_disk(spec, lambda p: SimpleNamespace(free=150e9)) == (True, "150.0 GB free on /")
    ok, said = argus.probe_disk(spec, lambda p: SimpleNamespace(free=12.5e9))
    assert not ok and said == "12.5 GB free on /, under 20 GB"


def test_file_growth_fails_until_acknowledged(ac):
    sizes = {"size": 565}
    tools = argus.ProbeTools(http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
                             file_size=lambda p: sizes["size"])
    argus.register(ac, "crashlog", kind="probe", probe={"type": "file_growth", "path": "/tmp/x.log", "every": 60})
    run = lambda t: argus.probe(ac, tools, now=at(t))[0]
    assert run(0)["ok"]                        # the first look sets the baseline
    assert run(60)["ok"]
    sizes["size"] = 1130
    r = run(120)
    assert not r["ok"] and "grew by 565 bytes" in r["summary"]
    assert not run(180)["ok"]                  # still failing a minute later: nobody looked yet
    argus.acknowledge(ac, "crashlog", tools, now=at(200))
    argus.evaluate(ac, now=at(200))
    assert status(ac, "crashlog") == "up"
    assert run(260)["ok"]
    sizes["size"] = 10                         # truncated or rotated: the new size is the new normal
    assert run(320)["ok"] and run(380)["ok"]


def test_probes_run_on_their_own_intervals_and_count_as_check_ins(ac):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200)
    tools = argus.ProbeTools(http=httpx.Client(transport=httpx.MockTransport(handler)),
                             launchctl=lambda: LAUNCHCTL, disk_usage=lambda p: SimpleNamespace(free=100e9))
    argus.register(ac, "hs", kind="probe", probe={"type": "http", "url": "http://127.0.0.1:8888/health", "every": 60})
    argus.register(ac, "dk", kind="probe", probe={"type": "disk", "every": 300})
    argus.register(ac, "gateway", kind="probe", probe={"type": "launchd", "label": "com.example.gateway"})
    first = {r["slug"]: r["ok"] for r in argus.probe(ac, tools, now=T0)}
    assert first == {"dk": True, "gateway": True, "hs": True}  # running: its exit status 1 is the run before
    assert [r["slug"] for r in argus.probe(ac, tools, now=at(30))] == []
    assert [r["slug"] for r in argus.probe(ac, tools, now=at(60))] == ["gateway", "hs"]
    assert calls == ["/health", "/health"]
    argus.evaluate(ac, now=at(60))
    assert status(ac, "gateway") == "up" and status(ac, "hs") == "up"
    # A probe that stops running (the timers died) makes its service late, then down.
    svc = ac.execute("select * from argus_service where slug = 'hs'").fetchone()
    assert argus.status_of(svc, at(60 + 90 + 119)) == "up"   # every + one sweep + grace
    assert argus.status_of(svc, at(60 + 90 + 120)) == "late"
    assert argus.status_of(svc, at(60 + 90 + 240)) == "down"


def test_a_probe_that_breaks_is_a_failing_service_not_a_broken_monitor(ac):
    def boom():
        raise PermissionError("launchctl")
    tools = argus.ProbeTools(http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
                             launchctl=boom, disk_usage=lambda p: (_ for _ in ()).throw(OSError("disk")))
    argus.register(ac, "l", kind="probe", probe={"type": "launchd", "label": "a.b"})
    argus.register(ac, "d", kind="probe", probe={"type": "disk"})
    res = {r["slug"]: r for r in argus.probe(ac, tools, now=T0)}
    assert not res["l"]["ok"] and res["l"]["summary"] == "launchctl list failed"
    assert not res["d"]["ok"] and res["d"]["summary"] == "the probe failed: OSError"


# ---------------------------------------------------------------- the outbound heartbeat
BEAT_URL = "https://hc-ping.example/5f1e-secret-token-uuid"


def beat_client(*responses):
    seen = []
    queue = list(responses)

    def handler(request):
        seen.append(request)
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return httpx.Response(r)
    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_the_beat_counts_successes_and_failures_in_a_row(ac):
    http, seen = beat_client(200, 500, httpx.ConnectError("down"), httpx.ReadTimeout("slow"), 200)
    assert argus.beat(ac, BEAT_URL, http, now=at(0))["sent"]
    for t in (300, 600, 900):
        assert not argus.beat(ac, BEAT_URL, http, now=at(t))["sent"]
    st = argus.beat_state(ac, True)
    assert st["state"] == "failing" and st["failures"] == 3 and st["total_ok"] == 1 and st["total_failures"] == 3
    assert st["last_ok_at"] == at(0) and st["last_fail_at"] == at(900) and st["last_error"] == "no answer within 10 s"
    assert argus.beat(ac, BEAT_URL, http, now=at(1200))["sent"]
    st = argus.beat_state(ac, True)
    assert st["state"] == "ok" and st["failures"] == 0 and st["total_failures"] == 3 and st["last_ok_at"] == at(1200)
    # One plain GET, no payload.
    assert all(r.method == "GET" and r.content == b"" and str(r.url) == BEAT_URL for r in seen)


def test_the_beat_never_records_its_url_which_holds_the_token(ac):
    def leaky(request):
        raise RuntimeError(f"failed to reach {request.url}")
    http = httpx.Client(transport=httpx.MockTransport(leaky))
    res = argus.beat(ac, BEAT_URL, http, now=T0)
    st = argus.beat_state(ac, True)
    assert not res["sent"] and st["last_error"] == "RuntimeError" and "secret" not in str(st)


def test_a_beat_that_is_not_set_up_says_so_and_sends_nothing(ac):
    http, seen = beat_client()
    assert argus.beat(ac, None, http, now=T0) == {"sent": False, "state": "not set up"}
    assert argus.beat_state(ac, False)["state"] == "not set up"
    assert argus.beat_state(ac, True)["state"] == "not sent yet"
    assert not argus.beat(ac, "http://hc-ping.example/plain-text", http, now=T0)["sent"]  # the token needs https
    assert seen == []


def test_the_monitor_beats_every_n_minutes_and_sweeps_every_tick(ac, database):
    http, seen = beat_client(*([200] * 10))
    notes = Notes()
    mon = argus.Monitor(database, notifier=notes, beat_url=lambda: BEAT_URL, beat_client=http, beat_every=300,
                        tools=argus.ProbeTools(http=http, launchctl=lambda: LAUNCHCTL))
    argus.register(ac, "job", grace_seconds=0)
    argus.record(ac, "job", ok=True, expected_next_within=60, now=T0)
    for t in range(0, 601, 30):
        mon.tick(now=at(t))
    assert len(seen) == 3  # at 0, 300 and 600
    assert status(ac, "job") == "down" and notes == [("Argus: job is down", notes[0][1])]
    assert mon.ticks == 21 and mon.last_tick_at == at(600)


def test_a_failing_heartbeat_is_announced_once_after_three_failures_and_once_when_it_recovers(ac, database):
    http, seen = beat_client(500, 500, 500, 500, 200, 200)
    notes = Notes()
    mon = argus.Monitor(database, notifier=notes, beat_url=lambda: BEAT_URL, beat_client=http, beat_every=300,
                        tools=argus.ProbeTools(http=http, launchctl=lambda: LAUNCHCTL))
    for t in range(0, 1501, 300):
        mon.tick(now=at(t))
    assert len(seen) == 6
    titles = [n[0] for n in notes]
    assert titles == ["Argus: heartbeat failing", "Argus: heartbeat back"]
    assert "3 times in a row" in notes[0][1] and BEAT_URL not in notes[0][1]


def test_the_timers_are_off_unless_argus_json_turns_them_on(tmp_path):
    assert argus.load_settings(tmp_path) == {}
    (tmp_path / "argus.json").write_text('{"enabled": true, "beat_every_minutes": 5}')
    assert argus.load_settings(tmp_path)["enabled"] is True
    (tmp_path / "argus.json").write_text("{not json")
    assert argus.load_settings(tmp_path) == {"enabled": False}


# ---------------------------------------------------------------- pruning
def test_check_ins_older_than_30_days_are_pruned_and_the_daily_counts_stay(ac):
    argus.register(ac, "job")
    for days in (45, 31, 29, 1):
        argus.record(ac, "job", ok=days != 29, expected_next_within=60, now=T0 - timedelta(days=days))
    assert argus.prune(ac, now=T0) == 2
    assert [r["at"] for r in ac.execute("select at from argus_checkin order by at")] == \
        [T0 - timedelta(days=29), T0 - timedelta(days=1)]
    daily = ac.execute("select sum(ok) ok, sum(fail) fail, count(*) days from argus_daily").fetchone()
    assert (daily["ok"], daily["fail"], daily["days"]) == (3, 1, 4)


# ---------------------------------------------------------------- the HTTP endpoints
def test_a_check_in_needs_the_token(ac, database, tmp_path):
    argus.register(ac, "vs")
    c = client(database, tmp_path)
    url = "/argus/checkin/vs?expected_next_within=300"
    assert c.post(url).status_code == 401
    assert c.post(url, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.post(url + "&token=wrong").status_code == 401
    assert c.post("/argus/checkin/vs", data={"token": TOKEN, "expected_next_within": "300"}).status_code == 401
    assert c.post(url, headers={"Authorization": f"Bearer {TOKEN}"}).json()["status"] == "up"
    assert c.post(url + f"&token={TOKEN}").status_code == 200
    assert c.post("/argus/fail/vs", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert ac.execute("select count(*) n from argus_checkin").fetchone()["n"] == 2


def test_the_token_is_read_once_and_again_at_most_once_a_minute():
    reads, now = [], [0.0]
    store = {"token": None}

    def reader():
        reads.append(now[0])
        return store["token"]
    check = argus.TokenCheck(reader, recheck=60, clock=lambda: now[0])
    assert not check.configured() and reads == [0.0]
    store["token"] = "new-token"               # the owner sets it up while the server runs
    now[0] = 30
    assert not check.configured() and len(reads) == 1
    now[0] = 61
    assert check.configured() and check("new-token") and len(reads) == 2
    for guess in ("a", "b", "c"):               # wrong guesses do not become Keychain reads
        assert not check(guess)
    assert len(reads) == 2
    store["token"] = "rotated"
    now[0] = 200
    assert check("rotated") and len(reads) == 3 and not check("new-token")


def test_without_a_token_in_the_keychain_every_check_in_is_refused(ac, database, tmp_path):
    argus.register(ac, "vs")
    c = client(database, tmp_path, token=None)
    r = c.post("/argus/checkin/vs?expected_next_within=300", headers={"Authorization": "Bearer "})
    assert r.status_code == 503 and "talos argus token" in r.json()["error"]


def test_a_check_in_for_an_unknown_slug_is_refused_but_only_after_the_token(ac, database, tmp_path):
    c = client(database, tmp_path)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert c.post("/argus/checkin/nobody?expected_next_within=60").status_code == 401  # no enumeration
    assert c.post("/argus/checkin/nobody?expected_next_within=60", headers=auth).status_code == 404
    assert c.post("/argus/fail/nobody", headers=auth).status_code == 404
    assert c.post("/argus/checkin/Not%20A%20Slug?expected_next_within=60", headers=auth).status_code == 400


def test_curl_style_check_ins_and_failures_are_recorded(ac, database, tmp_path):
    argus.register(ac, "vs", "backup-script")
    notes = Notes()
    c = client(database, tmp_path, notes=notes)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    r = c.post("/argus/checkin/vs", headers=auth, data={"ok": "true", "expected_next_within": "300",
                                                        "summary": "6 repos\nin sync\x1b[31m"})
    assert r.status_code == 200 and r.json()["status"] == "up"
    r = c.post("/argus/fail/vs", headers=auth, data={"reason": "cowork parked: conflicts unresolved"})
    assert r.json()["status"] == "failing"
    assert notes == [("Argus: backup-script is failing", "cowork parked: conflicts unresolved")]
    r = c.post("/argus/checkin/vs", headers=auth, json={"expected_next_within": 3600, "summary": "json works"})
    assert r.json()["status"] == "up"
    assert c.post("/argus/checkin/vs", headers=auth, data={"ok": "maybe", "expected_next_within": "1"}).status_code == 400
    assert c.post("/argus/checkin/vs", headers=auth, data={"summary": "no deadline"}).status_code == 400
    assert c.post("/argus/checkin/vs", headers=auth, data={"summary": "x" * 5000}).status_code == 413
    rows = ac.execute("select ok, summary from argus_checkin order by id").fetchall()
    assert [(r["ok"], r["summary"]) for r in rows] == [
        (True, "6 repos in sync [31m"), (False, "cowork parked: conflicts unresolved"), (True, "json works")]


def test_check_ins_obey_the_host_check(ac, database, tmp_path):
    argus.register(ac, "vs")
    c = client(database, tmp_path)
    r = c.post("/argus/checkin/vs?expected_next_within=60", headers={"Authorization": f"Bearer {TOKEN}",
                                                                     "host": "evil.example"})
    assert r.status_code == 400


def test_the_status_page_data_and_its_pause_button(ac, database, tmp_path):
    argus.seed(ac)
    argus.record(ac, "talos-backup", ok=True, expected_next_within=300, summary="backup made")
    c = client(database, tmp_path)
    st = c.get("/argus/status").json()
    by = {s["slug"]: s for s in st["services"]}
    assert by["talos-backup"]["status"] == "up" and by["talos-web"]["status"] == "unknown"
    assert by["disk"]["probe"]["min_free_gb"] == 20
    assert len(by["talos-backup"]["history"]) == 14 and by["talos-backup"]["history"][-1]["ok"] == 1
    assert st["beat"]["state"] == "not set up" and st["monitor"]["timers"] is False and not st["all_up"]
    assert c.post("/api/argus/talos-backup/pause").status_code == 403  # the page's writes carry X-Talos
    r = c.post("/api/argus/talos-backup/pause", headers={"X-Talos": "1"})
    assert {s["slug"]: s["status"] for s in r.json()["services"]}["talos-backup"] == "paused"
    c.post("/api/argus/talos-backup/resume", headers={"X-Talos": "1"})
    assert {s["slug"]: s["status"] for s in c.get("/argus/status").json()["services"]}["talos-backup"] == "up"
    assert c.post("/api/argus/nobody/pause", headers={"X-Talos": "1"}).status_code == 404
    assert c.post("/api/argus/disk/ack", headers={"X-Talos": "1"}).status_code == 400


# ---------------------------------------------------------------- the never-raise guard
def test_ping_never_raises_into_its_caller(ac, database, monkeypatch):
    argus.register(ac, "talos-sync")
    assert argus.ping(database, "talos-sync", expected_next_within=300, summary="ok")
    assert not argus.ping(database, "nobody", expected_next_within=300)
    assert not argus.ping(database, "talos-sync")                          # no deadline
    assert not argus.ping(database, "talos-sync", expected_next_within="soon")
    assert not argus.ping(database, "Bad Slug!", expected_next_within=300)
    assert not argus.ping("host=/nonexistent port=1 dbname=x", "talos-sync", expected_next_within=300, timeout=1)
    for exc in (RuntimeError("boom"), MemoryError(), TimeoutError(), ValueError()):
        def broken(*a, _exc=exc, **kw):
            raise _exc
        monkeypatch.setattr(argus, "record", broken)
        assert argus.ping(database, "talos-sync", expected_next_within=300) is False
    monkeypatch.setattr(argus.log, "warning", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("logging")))
    assert argus.ping(database, "talos-sync", expected_next_within=300) is False


def _sync_args():
    return argparse.Namespace(accounts=[], limit=None, no_extract=False, then_rules=False)


def test_a_full_sync_checks_in_as_talos_sync(conn, ac, database, tmp_path):
    conn.execute("update account set enabled = false")
    conn.commit()
    argus.register(ac, "talos-sync")
    cli.cmd_sync(_sync_args(), Settings(home=tmp_path, dsn=database))
    row = ac.execute("select * from argus_checkin").fetchone()
    assert row["ok"] and row["summary"] == "0 accounts, 0 new messages"
    assert (row["expected_next_at"] - row["at"]).total_seconds() == argus.SYNC_EVERY


def test_a_failure_to_record_the_check_in_never_fails_the_sync(conn, ac, database, tmp_path, monkeypatch):
    conn.execute("update account set enabled = false")
    conn.commit()
    monkeypatch.setattr(argus, "record", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("argus is broken")))
    cli.cmd_sync(_sync_args(), Settings(home=tmp_path, dsn=database))  # returns normally: no raise, no exit(1)
    monkeypatch.undo()
    cli.cmd_sync(_sync_args(), Settings(home=tmp_path, dsn=database))  # talos-sync not registered: also fine


# The build holds the promise as well: outside argus.py, the web app and the argus command, Talos
# reaches Argus only through ping(), and every shell line we hand out gives up in 3 s and swallows
# its failure.
ALLOWED = {"argus.py", "app.py"}


def _code(path: Path) -> str:
    return re.sub(r'"""(.|\n)*?"""', '""', path.read_text(encoding="utf-8"))


def test_talos_calls_argus_only_through_ping_outside_argus_itself():
    src = ROOT / "src" / "talos"
    problems = []
    for p in sorted(src.rglob("*.py")):
        if p.name in ALLOWED:
            continue
        text = _code(p)
        if p.name == "cli.py":  # the argus command itself may use the whole module, and the web
            # command its notifier (a session made on this Mac is announced, docs/security.md)
            text = re.sub(r"\ndef cmd_argus\(.*?(?=\ndef )", "\n", text, flags=re.S)
            text = re.sub(r"\ndef cmd_web\(.*?(?=\ndef )", "\n", text, flags=re.S)
        problems += [f"{p.name}: {m.group(0)}" for m in re.finditer(r"\bargus\.(?!ping\(|SYNC_EVERY\b)\w+", text)]
    assert problems == []
    assert "argus.ping(" in _code(src / "cli.py").split("def cmd_sync(")[1].split("\ndef ")[0]


def _curl_lines(text: str) -> list[str]:
    # A command line, not prose that mentions curl: the word, a space, then an option.
    return [line.strip() for line in text.splitlines() if re.match(r"\s*(\$ )?curl\s+-", line)]


def test_every_check_in_line_gives_up_in_3_seconds_and_swallows_failure():
    lines = cli._curl_lines("backup-script")[1:] + _curl_lines((ROOT / "docs" / "argus.md").read_text(encoding="utf-8"))
    assert len(lines) >= 4
    for line in lines:
        assert " -m 3 " in line and line.endswith("|| true") and "-fsS" in line, line


def test_the_guard_catches_what_it_claims_to():
    assert _curl_lines("curl -fsS https://x/checkin/a\n  curl -fsS -m 3 x || true\ncurl's time_total") == \
        ["curl -fsS https://x/checkin/a", "curl -fsS -m 3 x || true"]
    bad = "from talos import argus\nargus.record(conn, 'x', ok=True)\nargus.ping(dsn, 'x')\n"
    assert [m.group(0) for m in re.finditer(r"\bargus\.(?!ping\(|SYNC_EVERY\b)\w+", bad)] == ["argus.record"]
