"""Noticing that a program has just started (SPEC 9).

SPEC 9 asks for kernel process events with a two-second poll as a fallback, and
Milestone 0 spike 3 measured the difference: the kernel reports an exec 0.8 ms
after launch, while polling can be two seconds late. For a blocked application
that gap is the difference between a window that never appears and one the user
gets to look at.

The fallback is not decoration. Subscribing needs ``CAP_NET_ADMIN`` and a
kernel built with ``CONFIG_PROC_EVENTS``, and neither is guaranteed. When the
subscription cannot be made, the watcher says so once and polls instead, and
everything above it works the same way.

Only the standard library is used, as SPEC 5.4 requires of root daemons, so the
netlink messages are packed and unpacked here by hand.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import struct
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

log = logging.getLogger("anchor-blockerd")

NETLINK_CONNECTOR: Final = 11
CN_IDX_PROC: Final = 1
CN_VAL_PROC: Final = 1
PROC_CN_MCAST_LISTEN: Final = 1
PROC_CN_MCAST_IGNORE: Final = 2

PROC_EVENT_EXEC: Final = 0x00000002

_NLMSGHDR: Final = struct.Struct("=IHHII")
_CN_MSG: Final = struct.Struct("=IIIIHH")

#: How often to look when the kernel will not tell us (SPEC 9).
POLL_SECONDS: Final = 2.0

#: Called with the pid of a process that has just executed something.
LaunchHandler = Callable[[int], None]


def _subscribe_message(operation: int) -> bytes:
    """A netlink message asking the connector to start or stop sending."""
    body = struct.pack("=I", operation)
    cn_msg = _CN_MSG.pack(CN_IDX_PROC, CN_VAL_PROC, 0, 0, len(body), 0)
    payload = cn_msg + body
    header = _NLMSGHDR.pack(
        _NLMSGHDR.size + len(payload),
        0x0003,  # NLMSG_DONE
        0,
        0,
        os.getpid(),
    )
    return header + payload


class ProcessWatcher:
    """Reports pids as they execute, by kernel event or by polling."""

    def __init__(
        self,
        on_launch: LaunchHandler,
        *,
        poll_seconds: float = POLL_SECONDS,
        proc: Path = Path("/proc"),
    ) -> None:
        self._on_launch = on_launch
        self._poll_seconds = poll_seconds
        self._proc = proc
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None
        self._using_events = False
        self._seen: set[int] = set()

    @property
    def using_events(self) -> bool:
        """Whether the kernel is telling us, rather than us asking."""
        return self._using_events

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._using_events = self._subscribe()
        if not self._using_events:
            log.info(
                "watching process launches by polling every %.0f s; the kernel's "
                "own events need CAP_NET_ADMIN and CONFIG_PROC_EVENTS",
                self._poll_seconds,
            )
            # Take the baseline here rather than on the new thread, so that a
            # caller who starts an application straight after start() returns
            # gets told about it instead of it counting as already running.
            self._seen = set(_pids(self._proc))

        target = self._watch_events if self._using_events else self._poll
        self._thread = threading.Thread(target=target, name="anchor-procwatch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._socket is not None:
            # Telling the connector to stop is a courtesy; closing the socket
            # is what ends the subscription, so a failure here is fine. The
            # closed socket is also what wakes the thread out of recv().
            with contextlib.suppress(OSError):
                self._socket.send(_subscribe_message(PROC_CN_MCAST_IGNORE))
            self._socket.close()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        # Only once the thread is gone, so it never reads a socket that has
        # been taken out from under it.
        self._socket = None

    def __enter__(self) -> ProcessWatcher:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- the kernel's way ------------------------------------------------

    def _subscribe(self) -> bool:
        try:
            sock = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, NETLINK_CONNECTOR)
        except OSError as error:
            log.debug("no netlink connector socket: %s", error)
            return False

        try:
            sock.bind((os.getpid(), CN_IDX_PROC))
            sock.send(_subscribe_message(PROC_CN_MCAST_LISTEN))
        except OSError as error:
            log.debug("could not subscribe to process events: %s", error)
            sock.close()
            return False

        sock.settimeout(0.5)
        self._socket = sock
        return True

    def _watch_events(self) -> None:
        sock = self._socket
        assert sock is not None
        while not self._stopping.is_set():
            try:
                data = sock.recv(4096)
            except TimeoutError:
                continue
            except OSError:
                break

            pid = _exec_pid(data)
            if pid is not None:
                self._report(pid)

    # -- the fallback ----------------------------------------------------

    def _poll(self) -> None:
        """Report pids that were not there last time we looked.

        Two seconds late is worse than 0.8 ms, and it is what SPEC 9 asks for
        where the kernel will not cooperate. Processes that start and finish
        between two looks are missed, which is acceptable: an application a
        user opened is still running when they see it.
        """
        while not self._stopping.wait(self._poll_seconds):
            current = set(_pids(self._proc))
            for pid in sorted(current - self._seen):
                self._report(pid)
            self._seen = current

    def _report(self, pid: int) -> None:
        try:
            self._on_launch(pid)
        except Exception:
            # One bad handler must not end the watch; missing every later
            # launch would be a far worse failure than missing this one.
            log.exception("the launch handler raised for pid %d", pid)


def _exec_pid(message: bytes) -> int | None:
    """The pid from an exec event, or ``None`` for anything else."""
    offset = _NLMSGHDR.size + _CN_MSG.size
    if len(message) < offset + 8:
        return None

    what = struct.unpack_from("=I", message, offset)[0]
    if what != PROC_EVENT_EXEC:
        return None

    # what, cpu, timestamp_ns, then the event body.
    body = offset + 16
    if len(message) < body + 8:
        return None
    pid: int = struct.unpack_from("=i", message, body)[0]
    return pid


def _pids(proc: Path) -> list[int]:
    try:
        return [int(entry.name) for entry in proc.iterdir() if entry.name.isdigit()]
    except OSError:
        return []


def wait_for(condition: Callable[[], bool], *, timeout: float, interval: float = 0.05) -> bool:
    """Wait until ``condition`` holds. Used by tests and by the grace period."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()
