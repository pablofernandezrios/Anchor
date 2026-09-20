"""The process watcher against the real kernel connector (SPEC 9).

Milestone 0 spike 3 measured the kernel reporting an exec 0.8 ms after the
launch. That measurement is worth nothing if the daemon's own code path does
not work, so this exercises ``ProcessWatcher`` against real processes rather
than against a synthesised message.

Subscribing needs ``CAP_NET_ADMIN`` and ``CONFIG_PROC_EVENTS``; where they are
missing these tests skip, and the polling fallback covered by the unit tests is
what Anchor would use.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Iterator

import pytest

from anchor.blocker.watcher import ProcessWatcher, wait_for


class Collector:
    def __init__(self) -> None:
        self.pids: list[int] = []
        self._lock = threading.Lock()

    def __call__(self, pid: int) -> None:
        with self._lock:
            self.pids.append(pid)

    def saw(self, pid: int) -> bool:
        with self._lock:
            return pid in self.pids


@pytest.fixture
def collector() -> Collector:
    return Collector()


@pytest.fixture
def watcher(collector: Collector) -> Iterator[ProcessWatcher]:
    watcher = ProcessWatcher(collector)
    watcher.start()
    if not watcher.using_events:
        watcher.stop()
        pytest.skip("the kernel's process connector is not available here")
    try:
        yield watcher
    finally:
        watcher.stop()


def launch(*command: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class TestRealLaunches:
    def test_a_new_process_is_reported(self, watcher: ProcessWatcher, collector: Collector) -> None:
        child = launch("/bin/sleep", "5")
        try:
            assert wait_for(
                lambda: collector.saw(child.pid), timeout=5
            ), "the kernel never reported the exec"
        finally:
            child.kill()
            child.wait()

    def test_it_arrives_far_sooner_than_the_poll_would(
        self, watcher: ProcessWatcher, collector: Collector
    ) -> None:
        """Spike 3 measured 0.8 ms; anything near two seconds is the fallback."""
        started = time.monotonic()
        child = launch("/bin/sleep", "5")
        try:
            assert wait_for(lambda: collector.saw(child.pid), timeout=5, interval=0.001)
            elapsed = time.monotonic() - started
        finally:
            child.kill()
            child.wait()

        assert elapsed < 0.5, f"the event took {elapsed:.3f} s, which is poll territory"

    def test_every_one_of_a_burst_is_reported(
        self, watcher: ProcessWatcher, collector: Collector
    ) -> None:
        """A user opening several things at once must not slip through."""
        children = [launch("/bin/sleep", "5") for _ in range(5)]
        try:
            assert wait_for(
                lambda: all(collector.saw(child.pid) for child in children), timeout=5
            ), "the connector dropped a launch"
        finally:
            for child in children:
                child.kill()
                child.wait()

    def test_a_process_that_only_forks_is_not_a_launch(
        self, watcher: ProcessWatcher, collector: Collector
    ) -> None:
        """Anchor acts on exec: a fork is still the program that was running.

        The fork happens in a child interpreter rather than here, because
        forking a process with threads in it is a way to deadlock.
        """
        source = (
            "import os, sys, time\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    time.sleep(0.3)\n"
            "    os._exit(0)\n"
            "print(pid, flush=True)\n"
            "os.waitpid(pid, 0)\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", source], capture_output=True, text=True, check=True
        )
        forked = int(done.stdout.strip())
        time.sleep(0.2)

        assert not collector.saw(forked), "a fork without an exec was taken for a launch"

    def test_the_watcher_stops_cleanly(self, collector: Collector) -> None:
        watcher = ProcessWatcher(collector)
        watcher.start()
        if not watcher.using_events:
            watcher.stop()
            pytest.skip("the kernel's process connector is not available here")

        thread = watcher._thread  # noqa: SLF001
        assert thread is not None
        watcher.stop()

        assert not thread.is_alive(), "the reading thread outlived stop()"

    def test_nothing_is_reported_after_stopping(self, collector: Collector) -> None:
        watcher = ProcessWatcher(collector)
        watcher.start()
        if not watcher.using_events:
            watcher.stop()
            pytest.skip("the kernel's process connector is not available here")
        watcher.stop()

        child = launch(sys.executable, "-c", "pass")
        child.wait()
        time.sleep(0.2)

        assert not collector.saw(child.pid)
