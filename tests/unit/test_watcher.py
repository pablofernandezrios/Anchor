"""Noticing a launch, by kernel event or by polling (SPEC 9)."""

from __future__ import annotations

import logging
import struct
import threading
from pathlib import Path

import pytest

from anchor.blocker.watcher import (
    _CN_MSG,
    _NLMSGHDR,
    CN_IDX_PROC,
    PROC_CN_MCAST_IGNORE,
    PROC_CN_MCAST_LISTEN,
    PROC_EVENT_EXEC,
    LaunchHandler,
    ProcessWatcher,
    _exec_pid,
    _pids,
    _subscribe_message,
    wait_for,
)

PROC_EVENT_EXIT = 0x80000000


def event(what: int, pid: int) -> bytes:
    """A message shaped the way the kernel shapes one.

    ``struct proc_event`` is ``what``, ``cpu``, a 64-bit timestamp, then the
    union, so the pid sits sixteen bytes after ``what`` whatever the event.
    """
    payload = struct.pack("=IIQ", what, 0, 12345) + struct.pack("=ii", pid, pid)
    cn_msg = _CN_MSG.pack(CN_IDX_PROC, 1, 0, 0, len(payload), 0)
    header = _NLMSGHDR.pack(_NLMSGHDR.size + len(cn_msg) + len(payload), 0x0003, 0, 0, 0)
    return header + cn_msg + payload


class Collector:
    """Records the pids handed to it, and can be waited on."""

    def __init__(self) -> None:
        self.pids: list[int] = []
        self.seen = threading.Event()

    def __call__(self, pid: int) -> None:
        self.pids.append(pid)
        self.seen.set()


class TestTheSubscriptionMessage:
    def test_the_length_covers_the_whole_message(self) -> None:
        """A wrong length makes the kernel drop the request in silence."""
        message = _subscribe_message(PROC_CN_MCAST_LISTEN)
        declared = _NLMSGHDR.unpack_from(message)[0]

        assert declared == len(message)

    def test_it_asks_the_process_connector_specifically(self) -> None:
        message = _subscribe_message(PROC_CN_MCAST_LISTEN)
        idx, val = _CN_MSG.unpack_from(message, _NLMSGHDR.size)[:2]

        assert (idx, val) == (CN_IDX_PROC, 1)

    def test_the_operation_is_carried_in_the_body(self) -> None:
        message = _subscribe_message(PROC_CN_MCAST_LISTEN)
        body = message[_NLMSGHDR.size + _CN_MSG.size :]

        assert struct.unpack("=I", body)[0] == PROC_CN_MCAST_LISTEN


class TestReadingAnEvent:
    def test_an_exec_gives_up_its_pid(self) -> None:
        assert _exec_pid(event(PROC_EVENT_EXEC, 4321)) == 4321

    def test_any_other_event_is_ignored(self) -> None:
        """An exit carries a pid too, and reporting it would be a launch."""
        assert _exec_pid(event(PROC_EVENT_EXIT, 4321)) is None

    def test_a_truncated_message_is_ignored_rather_than_unpacked(self) -> None:
        full = event(PROC_EVENT_EXEC, 4321)

        assert _exec_pid(full[:40]) is None
        assert _exec_pid(b"") is None

    def test_a_message_that_stops_before_the_pid_is_ignored(self) -> None:
        """The header says exec, but the body it promises is not there."""
        assert _exec_pid(event(PROC_EVENT_EXEC, 4321)[:56]) is None


class TestReadingProc:
    def test_only_the_numbered_entries_count(self, tmp_path: Path) -> None:
        (tmp_path / "17").mkdir()
        (tmp_path / "240").mkdir()
        (tmp_path / "self").mkdir()
        (tmp_path / "cpuinfo").write_text("", encoding="utf-8")

        assert sorted(_pids(tmp_path)) == [17, 240]

    def test_a_proc_that_is_not_there_is_empty_rather_than_an_error(self) -> None:
        assert _pids(Path("/nonexistent-proc")) == []


class TestPolling:
    """The fallback, driven by a directory that stands in for /proc."""

    def watcher(self, tmp_path: Path, handler: LaunchHandler) -> ProcessWatcher:
        watcher = ProcessWatcher(handler, poll_seconds=0.01, proc=tmp_path)
        # Refusing the subscription is what a kernel without CONFIG_PROC_EVENTS
        # does, and it is the only way to be sure this exercises the poll.
        watcher._subscribe = lambda: False  # type: ignore[method-assign]  # noqa: SLF001
        return watcher

    def test_a_process_that_appears_is_reported(self, tmp_path: Path) -> None:
        (tmp_path / "100").mkdir()
        collector = Collector()

        with self.watcher(tmp_path, collector):
            (tmp_path / "101").mkdir()
            assert collector.seen.wait(2), "the new pid was never reported"

        assert collector.pids == [101]

    def test_processes_already_running_are_not_reported(self, tmp_path: Path) -> None:
        """Starting a session must not look like everything just launched."""
        for pid in (100, 101, 102):
            (tmp_path / str(pid)).mkdir()
        collector = Collector()

        with self.watcher(tmp_path, collector):
            assert not collector.seen.wait(0.2)

        assert collector.pids == []

    def test_the_baseline_is_taken_before_start_returns(self, tmp_path: Path) -> None:
        """Otherwise an application launched immediately after would be missed.

        The grace period (SPEC 7.1) starts a session and then leaves the user
        to open things; a baseline taken late would silently swallow the first
        launch, which is exactly the one that matters.
        """
        (tmp_path / "100").mkdir()
        collector = Collector()
        watcher = self.watcher(tmp_path, collector)
        watcher.start()
        try:
            (tmp_path / "101").mkdir()
            assert collector.seen.wait(2)
        finally:
            watcher.stop()

        assert collector.pids == [101]

    def test_a_pid_that_goes_away_and_comes_back_is_a_new_launch(self, tmp_path: Path) -> None:
        """Pids are recycled, and the second process is not the first."""
        (tmp_path / "100").mkdir()
        collector = Collector()
        watcher = self.watcher(tmp_path, collector)
        watcher._seen = {100, 101}  # noqa: SLF001
        watcher.start()
        try:
            # 101 is in the baseline, so it takes a scan without it for the
            # watcher to count its return as a launch.
            assert not collector.seen.wait(0.1)
            watcher._seen = {100}  # noqa: SLF001
            (tmp_path / "101").mkdir()
            assert collector.seen.wait(2)
        finally:
            watcher.stop()

        assert collector.pids == [101]

    def test_several_launches_between_two_looks_are_all_reported(self, tmp_path: Path) -> None:
        """Two seconds is long enough for a shell script to start a handful."""
        reported = threading.Semaphore(0)
        pids: list[int] = []

        def handler(pid: int) -> None:
            pids.append(pid)
            reported.release()

        watcher = self.watcher(tmp_path, handler)
        with watcher:
            for pid in (200, 201, 202):
                (tmp_path / str(pid)).mkdir()
            assert all(reported.acquire(timeout=2) for _ in range(3))

        assert sorted(pids) == [200, 201, 202]


class TestAHandlerThatMisbehaves:
    def test_one_failure_does_not_end_the_watch(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Missing every later launch is far worse than missing this one."""
        reported: list[int] = []
        second = threading.Event()

        def handler(pid: int) -> None:
            reported.append(pid)
            if len(reported) == 1:
                raise RuntimeError("the enforcement path is on fire")
            second.set()

        watcher = ProcessWatcher(handler, poll_seconds=0.01, proc=tmp_path)
        watcher._subscribe = lambda: False  # type: ignore[method-assign]  # noqa: SLF001
        with caplog.at_level(logging.ERROR, logger="anchor-blockerd"), watcher:
            (tmp_path / "101").mkdir()
            (tmp_path / "102").mkdir()
            assert second.wait(2), "the watch stopped at the first failure"

        assert reported[:2] == [101, 102]
        assert "101" in caplog.text


class TestStopping:
    def test_stopping_twice_is_harmless(self, tmp_path: Path) -> None:
        """systemd can send a second SIGTERM before the first one lands."""
        watcher = ProcessWatcher(lambda pid: None, poll_seconds=0.01, proc=tmp_path)
        watcher.start()
        watcher.stop()
        watcher.stop()

    def test_the_thread_really_ends(self, tmp_path: Path) -> None:
        watcher = ProcessWatcher(lambda pid: None, poll_seconds=0.01, proc=tmp_path)
        watcher.start()
        thread = watcher._thread  # noqa: SLF001
        assert thread is not None
        watcher.stop()

        assert not thread.is_alive()

    def test_it_works_as_a_context_manager(self, tmp_path: Path) -> None:
        with ProcessWatcher(lambda pid: None, poll_seconds=0.01, proc=tmp_path) as watcher:
            assert watcher._thread is not None  # noqa: SLF001
            thread = watcher._thread  # noqa: SLF001

        assert not thread.is_alive()


class TestFallingBack:
    def test_a_refused_subscription_leaves_a_working_watcher(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CAP_NET_ADMIN is not guaranteed, and SPEC 9 wants the poll anyway."""
        monkeypatch.setattr(ProcessWatcher, "_subscribe", lambda self: False)
        (tmp_path / "100").mkdir()
        collector = Collector()

        with ProcessWatcher(collector, poll_seconds=0.01, proc=tmp_path) as watcher:
            assert not watcher.using_events
            (tmp_path / "101").mkdir()
            assert collector.seen.wait(2)

        assert collector.pids == [101]

    def test_a_socket_that_cannot_be_made_is_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A kernel without CONFIG_PROC_EVENTS refuses the socket outright."""

        def refuse(*args: object, **kwargs: object) -> None:
            raise OSError("no netlink here")

        monkeypatch.setattr("anchor.blocker.watcher.socket.socket", refuse)
        watcher = ProcessWatcher(lambda pid: None, poll_seconds=0.01, proc=tmp_path)

        assert watcher._subscribe() is False  # noqa: SLF001

    def test_a_subscription_the_kernel_refuses_closes_the_socket(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Without CAP_NET_ADMIN the socket opens and the send is denied.

        This is the unprivileged case, and leaking the descriptor for the life
        of the daemon would be the wrong way to notice it.
        """

        class Denied:
            def __init__(self) -> None:
                self.closed = False

            def bind(self, address: object) -> None:
                pass

            def send(self, data: bytes) -> int:
                raise PermissionError("operation not permitted")

            def close(self) -> None:
                self.closed = True

        denied = Denied()
        monkeypatch.setattr("anchor.blocker.watcher.socket.socket", lambda *a, **k: denied)
        watcher = ProcessWatcher(lambda pid: None, poll_seconds=0.01, proc=tmp_path)

        assert watcher._subscribe() is False  # noqa: SLF001
        assert denied.closed
        assert watcher._socket is None  # noqa: SLF001

    def test_the_fallback_is_chosen_when_the_kernel_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """start() must not leave the daemon watching nothing at all."""

        def refuse(*args: object, **kwargs: object) -> None:
            raise OSError("no netlink here")

        monkeypatch.setattr("anchor.blocker.watcher.socket.socket", refuse)
        collector = Collector()

        with ProcessWatcher(collector, poll_seconds=0.01, proc=tmp_path) as watcher:
            assert not watcher.using_events
            (tmp_path / "101").mkdir()
            assert collector.seen.wait(2)


class TestReadingTheSocket:
    """The event loop, driven by a socket that tests control."""

    class FakeSocket:
        def __init__(self, replies: list[object]) -> None:
            self.replies = replies
            self.sent: list[bytes] = []
            self.closed = False

        def recv(self, size: int) -> bytes:
            if not self.replies:
                raise OSError("closed")
            reply = self.replies.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            assert isinstance(reply, bytes)
            return reply

        def send(self, data: bytes) -> int:
            self.sent.append(data)
            return len(data)

        def close(self) -> None:
            self.closed = True

    def run(self, replies: list[object]) -> tuple[list[int], FakeSocket]:
        collector = Collector()
        watcher = ProcessWatcher(collector)
        sock = self.FakeSocket(replies)
        watcher._socket = sock  # type: ignore[assignment]  # noqa: SLF001
        watcher._watch_events()  # noqa: SLF001
        return collector.pids, sock

    def test_an_exec_reaches_the_handler(self) -> None:
        pids, _ = self.run([event(PROC_EVENT_EXEC, 909)])
        assert pids == [909]

    def test_a_timeout_is_not_the_end_of_the_watch(self) -> None:
        """The socket has a timeout so that stop() is noticed promptly."""
        pids, _ = self.run([TimeoutError(), event(PROC_EVENT_EXEC, 909), TimeoutError()])
        assert pids == [909]

    def test_a_closed_socket_ends_the_loop(self) -> None:
        pids, _ = self.run([OSError("closed"), event(PROC_EVENT_EXEC, 909)])
        assert pids == []

    def test_events_that_are_not_execs_are_passed_over(self) -> None:
        pids, _ = self.run([event(PROC_EVENT_EXIT, 1), b"", event(PROC_EVENT_EXEC, 909)])
        assert pids == [909]

    def test_stopping_unsubscribes_before_closing(self) -> None:
        watcher = ProcessWatcher(lambda pid: None)
        sock = self.FakeSocket([])
        watcher._socket = sock  # type: ignore[assignment]  # noqa: SLF001
        watcher.stop()

        assert sock.closed
        body = sock.sent[0][_NLMSGHDR.size + _CN_MSG.size :]
        assert struct.unpack("=I", body)[0] == PROC_CN_MCAST_IGNORE

    def test_a_send_that_fails_does_not_stop_the_close(self) -> None:
        """The daemon is shutting down; there is nobody left to tell."""

        class Broken(TestReadingTheSocket.FakeSocket):
            def send(self, data: bytes) -> int:
                raise OSError("gone")

        watcher = ProcessWatcher(lambda pid: None)
        sock = Broken([])
        watcher._socket = sock  # type: ignore[assignment]  # noqa: SLF001
        watcher.stop()

        assert sock.closed


class TestWaitFor:
    def test_it_returns_as_soon_as_the_condition_holds(self) -> None:
        calls = []

        def condition() -> bool:
            calls.append(1)
            return len(calls) >= 2

        assert wait_for(condition, timeout=2, interval=0.01)
        assert len(calls) == 2

    def test_it_gives_up_and_says_so(self) -> None:
        assert not wait_for(lambda: False, timeout=0.05, interval=0.01)

    def test_a_condition_that_is_already_true_does_not_wait(self) -> None:
        assert wait_for(lambda: True, timeout=0.01, interval=10)

    def test_a_zero_timeout_still_checks_once(self) -> None:
        """Otherwise a condition that already holds would report a timeout."""
        assert wait_for(lambda: True, timeout=0, interval=0.01)
