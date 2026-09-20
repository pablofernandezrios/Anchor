"""Closing the applications a session blocks (SPEC 7.1, 9).

Two moments, and they are deliberately not the same.

At the start of a session the applications already open may hold work nobody
has saved. SPEC 7.1 gives them two minutes and a notification, then ``SIGTERM``,
then ``SIGKILL`` ten seconds later. Anchor is friction, not a trap: taking
somebody's unsaved document is not friction, it is damage.

Once that grace has passed, SPEC 9 says a blocked application is killed
immediately on launch. There is nothing to save in a program that has just
started, so it gets the same escalation without the wait.

Nothing here decides what is blocked. The engine says which applications a
session covers and how much grace is left; this module carries it out.
"""

from __future__ import annotations

import functools
import logging
import os
import signal
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from anchor.blocker.apps import InstalledApp, discover
from anchor.blocker.processes import (
    PROC,
    RunningProcess,
    find_matches,
    matches,
    read_process,
)

log = logging.getLogger("anchor-blockerd")

#: How long the applications open at session start have to save (SPEC 7.1).
GRACE_SECONDS: float = 120.0

#: How long after SIGTERM before SIGKILL (SPEC 7.1).
KILL_AFTER_SECONDS: float = 10.0

#: How often to look for blocked applications that are somehow still running.
#: The watcher reports launches, and this is the belt to its braces: a missed
#: kernel event would otherwise leave a blocked application open all session.
SWEEP_SECONDS: float = 10.0

#: ``os.kill``, or something a test can watch.
Signaller = Callable[[int, int], None]

#: Runs a piece of work that must not block the caller.
Spawn = Callable[[Callable[[], None]], None]


@dataclass(frozen=True, slots=True)
class Closure:
    """One application Anchor closed, and how hard it had to push."""

    app_id: str
    name: str
    pids: tuple[int, ...]
    killed: bool
    """Whether ``SIGKILL`` was needed after ``SIGTERM`` was ignored."""


def is_alive(pid: int, *, proc: Path = PROC) -> bool:
    """Whether the process still exists.

    Read from ``/proc`` rather than with signal 0, because a zombie answers
    signal 0 and would keep the escalation waiting for a process that has
    already died.
    """
    try:
        status = (proc / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    # "pid (comm) state ...", and comm can contain spaces and brackets.
    closing = status.rfind(")")
    state = status[closing + 2 : closing + 3] if closing != -1 else ""
    return state not in ("Z", "X", "")


def _spawn(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="anchor-terminate", daemon=True).start()


def escalate(
    pids: Iterable[int],
    *,
    kill_after: float = KILL_AFTER_SECONDS,
    signaller: Signaller = os.kill,
    alive: Callable[[int], bool] = is_alive,
    sleep: Callable[[float], None] = time.sleep,
    interval: float = 0.1,
) -> bool:
    """Ask every process to go, then insist. Returns whether it had to insist.

    Every process is asked first and only then waited on. Asking one and
    waiting ten seconds before asking the next would take ten seconds per
    window, and the user would watch their applications close one at a time
    long after the session began.
    """
    remaining = [pid for pid in pids if _signal(pid, signal.SIGTERM, signaller)]
    if not remaining:
        return False

    deadline = time.monotonic() + kill_after
    while time.monotonic() < deadline:
        remaining = [pid for pid in remaining if alive(pid)]
        if not remaining:
            return False
        sleep(min(interval, max(0.0, deadline - time.monotonic())))

    stubborn = [pid for pid in remaining if alive(pid)]
    for pid in stubborn:
        _signal(pid, signal.SIGKILL, signaller)
    return bool(stubborn)


def _signal(pid: int, number: int, signaller: Signaller) -> bool:
    """Send one signal. Returns whether there was anything to send it to."""
    try:
        signaller(pid, number)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Root may still be refused by a sandbox or by a pid namespace. It is
        # worth saying so, because the application stays open.
        log.warning("not allowed to signal pid %d", pid)
        return False
    except OSError as error:
        log.warning("could not signal pid %d: %s", pid, error)
        return False
    return True


class AppEnforcer:
    """Keeps the blocked applications closed, once their grace has run out."""

    def __init__(
        self,
        *,
        catalogue: Callable[[], list[InstalledApp]] = discover,
        on_grace: Callable[[list[InstalledApp], float], None] | None = None,
        on_closed: Callable[[list[Closure], str], None] | None = None,
        signaller: Signaller = os.kill,
        proc: Path = PROC,
        kill_after: float = KILL_AFTER_SECONDS,
        sweep_seconds: float = SWEEP_SECONDS,
        spawn: Spawn = _spawn,
    ) -> None:
        self._catalogue = catalogue
        self._on_grace = on_grace
        self._on_closed = on_closed
        self._signaller = signaller
        self._proc = proc
        self._kill_after = kill_after
        self._sweep_seconds = sweep_seconds
        self._spawn = spawn

        self._lock = threading.Lock()
        self._wanted: frozenset[str] = frozenset()
        self._apps: list[InstalledApp] = []
        self._enforcing = False
        self._announced = False
        self._last_sweep = 0.0

    @property
    def enforcing(self) -> bool:
        """Whether the grace has passed and launches are being closed."""
        return self._enforcing

    @property
    def apps(self) -> list[InstalledApp]:
        """The installed applications this session blocks."""
        return list(self._apps)

    # -- what the daemon calls ------------------------------------------

    def update(self, app_ids: frozenset[str], grace_remaining: float) -> None:
        """Take in what the engine says, and act on it.

        Called on every poll, so it must be cheap when nothing has changed.
        """
        changed = self._resolve(app_ids)

        if grace_remaining > 0:
            self._enforcing = False
            self._announce(grace_remaining)
            return

        # The grace has passed. Anything matching goes, whether it was open
        # before the session or started during the wait.
        started = not self._enforcing
        self._enforcing = True
        if started or changed or self._due_a_sweep():
            self.sweep()

    def stop(self) -> None:
        """The session ended. Nothing is blocked until another one starts."""
        with self._lock:
            self._wanted = frozenset()
            self._apps = []
        self._enforcing = False
        self._announced = False
        self._last_sweep = 0.0

    def sweep(self) -> None:
        """Close every blocked application that is running right now."""
        self._last_sweep = time.monotonic()
        if not self._apps:
            return

        found = find_matches(self._apps, proc=self._proc)
        if found:
            self._close(found, reason="running")

    def on_launch(self, pid: int) -> None:
        """A process has just executed something. Close it if it is blocked."""
        if not self._enforcing or not self._apps:
            return

        process = read_process(pid, proc=self._proc)
        if process is None:
            # It finished before we looked, or it is not ours to read.
            return

        for app in self._apps:
            if matches(process, app):
                self._close([(process, app)], reason="launch")
                return

    # -- the work --------------------------------------------------------

    def _resolve(self, app_ids: frozenset[str]) -> bool:
        """Turn the engine's identifiers into installed applications."""
        with self._lock:
            if app_ids == self._wanted:
                return False
            self._wanted = app_ids

        if not app_ids:
            with self._lock:
                self._apps = []
            return True

        installed = {app.id: app for app in self._catalogue()}
        resolved = [installed[app_id] for app_id in sorted(app_ids) if app_id in installed]

        missing = sorted(app_ids - installed.keys())
        if missing:
            # Not an error: a profile may name an application that has been
            # uninstalled, or one installed only for another user.
            log.info("%d blocked application(s) are not installed here", len(missing))
            log.debug("not installed: %s", ", ".join(missing))

        with self._lock:
            self._apps = resolved
        return True

    def _announce(self, seconds: float) -> None:
        """Warn, once, about what is about to close (SPEC 7.1)."""
        if self._announced or not self._apps:
            return
        self._announced = True

        running = find_matches(self._apps, proc=self._proc)
        if not running:
            return

        # One entry per application, not per process: a browser with twenty
        # helper processes is one thing the user has open.
        doomed: list[InstalledApp] = []
        for _process, app in running:
            if app not in doomed:
                doomed.append(app)

        log.info("%d application(s) will close in %.0f s", len(doomed), seconds)
        if self._on_grace is not None:
            try:
                self._on_grace(doomed, seconds)
            except Exception:
                log.exception("could not report the grace period")

    def _close(self, found: Sequence[tuple[RunningProcess, InstalledApp]], *, reason: str) -> None:
        """Close each application, one worker per application.

        The escalation waits ten seconds for a process to leave, so it cannot
        run on the thread that found it: that thread is either the daemon's
        poll loop or the watcher, and neither can afford to stop listening.
        """
        by_app: dict[str, tuple[InstalledApp, list[int]]] = {}
        for process, app in found:
            by_app.setdefault(app.id, (app, []))[1].append(process.pid)

        for app, pids in by_app.values():
            log.info("closing %s (%d process(es))", app.name, len(pids))
            self._spawn(functools.partial(self._escalate, app, tuple(pids), reason))

    def _escalate(self, app: InstalledApp, pids: tuple[int, ...], reason: str) -> None:
        killed = escalate(
            pids,
            kill_after=self._kill_after,
            signaller=self._signaller,
            alive=lambda pid: is_alive(pid, proc=self._proc),
        )
        closure = Closure(app_id=app.id, name=app.name, pids=pids, killed=killed)
        if self._on_closed is not None:
            try:
                self._on_closed([closure], reason)
            except Exception:
                log.exception("could not report a closed application")

    def _due_a_sweep(self) -> bool:
        return time.monotonic() - self._last_sweep >= self._sweep_seconds
