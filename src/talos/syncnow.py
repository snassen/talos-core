"""Sync now: the mail accounts synced when the owner presses the button, between the 5-minute runs.

It starts the same `talos sync --then-rules` the launchd job runs, as its own process, so the web
server never holds a mail connection or waits on one. The per-account locks in cmd_sync keep it
from colliding with the 5-minute job: an account that job is syncing at that moment is skipped
and comes in with it. Teams is left out: its sync is slow and rate-limited, and it runs every
5 minutes anyway. Only mail accounts that are enabled can be asked for.

TeamsFast is Teams's own fast lane while the Teams place is open: the recent chats only, about every
20 seconds, in its own process as well. After the owner posts, it is boosted for a while: every few seconds, as an
answer usually comes within seconds in a chat.
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

LEFT_OUT = ("teams", "local")  # providers the button does not sync
_LINE = re.compile(r"^(?P<acct>[\w.-]+): (?:seen=(?P<seen>\d+) added=(?P<added>\d+).*|(?P<note>.+))$")


def mail_accounts(conn) -> list[dict]:
    return conn.execute("select id, display_name, provider from account where enabled and provider <> all(%s)"
                        " order by id", (list(LEFT_OUT),)).fetchall()


class SyncNow:
    """One manual sync at a time; the state lives in the web server's memory."""

    def __init__(self, home: Path, runner=subprocess.Popen, clock=time.time):
        self.home, self.runner, self.clock = home, runner, clock
        self.proc = None
        self.accounts: list[str] = []
        self.started = self.finished = None
        self.out: Path = home / "logs" / "sync-now.out"

    def start(self, accounts: list[str]) -> dict:
        if self.running():
            return self.state()
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.accounts, self.started, self.finished = list(accounts), self.clock(), None
        with open(self.out, "w") as f:
            self.proc = self.runner([sys.executable, "-m", "talos.cli", "sync", *accounts, "--then-rules"],
                                    stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        return self.state()

    def running(self) -> bool:
        if self.proc is None or self.proc.poll() is not None:
            if self.proc is not None and self.finished is None:
                self.finished = self.clock()
            return False
        return True

    def results(self) -> dict:
        """Per account what the run printed: added, or a note (skipped, FAILED …)."""
        try:
            lines = self.out.read_text(errors="replace").splitlines()
        except OSError:
            return {}
        res = {}
        for line in lines:
            m = _LINE.match(line.strip())
            if m and m["acct"] in self.accounts:
                res[m["acct"]] = {"added": int(m["added"])} if m["added"] else {"note": m["note"][:200]}
        return res

    def state(self) -> dict:
        running = self.running()
        return {"running": running, "accounts": self.accounts, "started": self.started, "finished": self.finished,
                "exit": None if running or self.proc is None else self.proc.returncode,
                "results": self.results() if self.proc is not None else {}}


TEAMS_RECENT = 20        # chats the fast lane looks at: those with the newest messages
TEAMS_FAST_EVERY = 15.0  # seconds between two fast-lane runs, however often the page asks
TEAMS_BOOST_EVERY = 5.0  # the same while boosted, after the owner posted
TEAMS_BOOST_FOR = 120.0  # seconds a post boosts the lane; each new post starts it again


class TeamsFast:
    """Teams's fast lane while the Teams place is open. The page asks about every 20 seconds; this starts
    `talos sync teams --recent N` as its own process, one at a time and not more often than TEAMS_FAST_EVERY.
    It finds the chats with new messages in one request and reads only those (about two seconds); channels stay
    with the 5-minute job. The per-account lock keeps it from colliding with that job: then it simply skips a turn.

    The boost: a post boosts the lane for TEAMS_BOOST_FOR seconds, in which it runs every TEAMS_BOOST_EVERY
    seconds instead, at the page's asking, and reads where the owner posted (the recent chats, or the one
    channel). Then it is back to normal by itself. It changes only how often the same read-only run starts;
    still one at a time."""

    def __init__(self, home: Path, runner=subprocess.Popen, clock=time.time):
        self.home, self.runner, self.clock = home, runner, clock
        self.proc = None
        self.started = self.finished = None
        self.boost_until = 0.0
        self.boost_channel: tuple[str, str] | None = None
        self.out: Path = home / "logs" / "teams-fast.out"

    def boost(self, channel: tuple[str, str] | None = None) -> None:
        """The owner posted (in a chat, or in this channel): look often for the answer for a while."""
        self.boost_until, self.boost_channel = self.clock() + TEAMS_BOOST_FOR, channel

    def boost_left(self) -> float:
        return max(0.0, self.boost_until - self.clock())

    def running(self) -> bool:
        if self.proc is None or self.proc.poll() is not None:
            if self.proc is not None and self.finished is None:
                self.finished = self.clock()
            return False
        return True

    def start(self, *, force: bool = False, channel: tuple[str, str] | None = None) -> dict:
        """channel=(team id, channel id): that one channel instead of the recent chats (after a post in it)."""
        boosted = self.boost_left() > 0
        every = TEAMS_BOOST_EVERY if boosted else TEAMS_FAST_EVERY
        if self.running() or (not force and self.started and self.clock() - self.started < every):
            return self.state()
        if channel is None and boosted:
            channel = self.boost_channel
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.started, self.finished = self.clock(), None
        what = ["--channel", f"{channel[0]}/{channel[1]}"] if channel else ["--recent", str(TEAMS_RECENT)]
        with open(self.out, "w") as f:
            self.proc = self.runner([sys.executable, "-m", "talos.cli", "sync", "teams", *what],
                                    stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
        return self.state()

    def state(self) -> dict:
        running = self.running()
        added = None
        if not running and self.proc is not None:
            try:
                m = _LINE.match((self.out.read_text(errors="replace").strip().splitlines() or [""])[-1])
                added = int(m["added"]) if m and m["added"] else None
            except OSError:
                pass
        return {"running": running, "started": self.started, "finished": self.finished, "added": added,
                "boost": round(self.boost_left())}
