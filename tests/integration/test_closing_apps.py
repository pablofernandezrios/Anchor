"""Closing real processes (SPEC 7.1, 9).

The unit tests drive the enforcer against a directory shaped like ``/proc``.
This drives it against the kernel's: real executables, real signals, real
``/proc/<pid>/stat``. It is the part that would still be wrong if the fake
``/proc`` were wrong in the same way.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from anchor.blocker.apps import AppKind, InstalledApp
from anchor.blocker.enforcement import AppEnforcer, Closure, escalate, is_alive
from anchor.blocker.watcher import wait_for

STUBBORN = (
    "import signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print('ready', flush=True)\n"
    "time.sleep(30)\n"
)


@pytest.fixture
def binary(tmp_path: Path) -> Path:
    """A copy of a real program, so only this test's processes match it."""
    source = shutil.which("sleep")
    assert source, "this test needs /usr/bin/sleep"
    target = tmp_path / "anchor-test-app"
    shutil.copy(source, target)
    return target


@pytest.fixture
def app(binary: Path) -> InstalledApp:
    return InstalledApp(
        id="test-app.desktop",
        name="Test App",
        kind=AppKind.NATIVE,
        exec_path=str(binary),
    )


@pytest.fixture
def running(binary: Path) -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    yield started
    for child in started:
        if child.poll() is None:
            child.kill()
        child.wait()


def launch(binary: Path, started: list[subprocess.Popen[bytes]]) -> subprocess.Popen[bytes]:
    child = subprocess.Popen([str(binary), "60"])
    started.append(child)
    # The process must be visible in /proc before anything looks for it.
    assert wait_for(lambda: is_alive(child.pid), timeout=5)
    return child


class Reports:
    def __init__(self) -> None:
        self.closed: list[tuple[str, str]] = []

    def __call__(self, closures: list[Closure], reason: str) -> None:
        for closure in closures:
            self.closed.append((closure.name, reason))


class TestIsAliveAgainstTheKernel:
    def test_a_real_process_is_alive(
        self, binary: Path, running: list[subprocess.Popen[bytes]]
    ) -> None:
        child = launch(binary, running)
        assert is_alive(child.pid)

    def test_a_real_zombie_is_not(self) -> None:
        """A zombie answers signal 0, which is why /proc is read instead."""
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            # Not reaped, so it stays a zombie until wait() below.
            assert wait_for(
                lambda: not is_alive(child.pid), timeout=5
            ), "a zombie was reported as alive"
            os.kill(child.pid, 0)  # and the kernel still accepts a signal for it
        finally:
            child.wait()

    def test_a_pid_that_never_existed_is_not(self) -> None:
        assert not is_alive(4_194_303)


class TestClosingRealApplications:
    def test_a_running_application_is_closed(
        self, app: InstalledApp, binary: Path, running: list[subprocess.Popen[bytes]]
    ) -> None:
        child = launch(binary, running)
        reports = Reports()
        enforcer = AppEnforcer(
            catalogue=lambda: [app],
            on_closed=reports,
            kill_after=5.0,
            spawn=lambda work: work(),
        )

        enforcer.update(frozenset({app.id}), grace_remaining=0.0)

        assert child.wait(timeout=5) == -signal.SIGTERM
        assert reports.closed == [("Test App", "running")]

    def test_it_is_left_alone_while_the_grace_runs(
        self, app: InstalledApp, binary: Path, running: list[subprocess.Popen[bytes]]
    ) -> None:
        """SPEC 7.1: two real minutes, with real unsaved work in them."""
        child = launch(binary, running)
        enforcer = AppEnforcer(catalogue=lambda: [app], spawn=lambda work: work())

        enforcer.update(frozenset({app.id}), grace_remaining=120.0)
        time.sleep(0.2)

        assert child.poll() is None

    def test_every_process_of_the_application_goes(
        self, app: InstalledApp, binary: Path, running: list[subprocess.Popen[bytes]]
    ) -> None:
        children = [launch(binary, running) for _ in range(3)]
        enforcer = AppEnforcer(catalogue=lambda: [app], kill_after=5.0, spawn=lambda work: work())

        enforcer.update(frozenset({app.id}), grace_remaining=0.0)

        for child in children:
            assert child.wait(timeout=5) == -signal.SIGTERM

    def test_a_launch_during_the_session_is_closed(
        self, app: InstalledApp, binary: Path, running: list[subprocess.Popen[bytes]]
    ) -> None:
        reports = Reports()
        enforcer = AppEnforcer(
            catalogue=lambda: [app],
            on_closed=reports,
            kill_after=5.0,
            spawn=lambda work: work(),
        )
        enforcer.update(frozenset({app.id}), grace_remaining=0.0)

        child = launch(binary, running)
        enforcer.on_launch(child.pid)

        assert child.wait(timeout=5) == -signal.SIGTERM
        assert reports.closed == [("Test App", "launch")]

    def test_something_else_is_not_touched(
        self, app: InstalledApp, running: list[subprocess.Popen[bytes]]
    ) -> None:
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        running.append(other)
        enforcer = AppEnforcer(catalogue=lambda: [app], spawn=lambda work: work())

        enforcer.update(frozenset({app.id}), grace_remaining=0.0)
        enforcer.on_launch(other.pid)
        time.sleep(0.2)

        assert other.poll() is None


class TestAProgramThatIgnoresSigterm:
    def test_it_is_killed_ten_seconds_later(self) -> None:
        """SPEC 7.1 ends with SIGKILL, and this is a program that needs it."""
        child = subprocess.Popen([sys.executable, "-c", STUBBORN], stdout=subprocess.PIPE)
        try:
            assert child.stdout is not None
            assert child.stdout.readline().strip() == b"ready"

            started = time.monotonic()
            killed = escalate([child.pid], kill_after=1.0)
            elapsed = time.monotonic() - started

            assert killed
            assert child.wait(timeout=5) == -signal.SIGKILL
            # It waited before insisting, rather than killing outright.
            assert elapsed >= 1.0
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()

    def test_a_program_that_leaves_politely_is_never_killed(
        self, binary: Path, running: list[subprocess.Popen[bytes]]
    ) -> None:
        child = launch(binary, running)

        assert not escalate([child.pid], kill_after=5.0)
        assert child.wait(timeout=5) == -signal.SIGTERM
