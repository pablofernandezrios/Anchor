"""Staying connected to the engine (SPEC 5.1, P4).

The agent is a user service and the engine is a system service, so they are
restarted independently: a package upgrade, a crash, a `systemctl restart`.
Whatever the reason, the agent has to come back on its own, because a top-bar
indicator that vanished when the engine restarted would be indistinguishable
from a session that ended — and that is the one lie Anchor must not tell.

So this reconnects, quietly and for as long as it takes, and asks for the
current status each time rather than waiting for something to happen. With no
session running the engine sends no events at all, and an agent that learned
only from events would show nothing for as long as nothing changed.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from anchor.cli.client import EngineClient, EngineUnreachableError
from anchor.protocol.messages import Event

log = logging.getLogger("anchor-agent")

#: How long to wait before the first reconnection, and the ceiling it doubles
#: towards. Thirty seconds is slow enough not to spin and fast enough that a
#: restarted engine is noticed before the user is.
FIRST_RETRY_SECONDS = 1.0
MAX_RETRY_SECONDS = 30.0

#: How long a read may block before the loop checks whether it was stopped.
READ_TIMEOUT_SECONDS = 2.0

StatusHandler = Callable[[dict[str, Any]], None]
EventHandler = Callable[[Event], None]
ConnectionHandler = Callable[[bool], None]


class EngineFeed:
    """The engine's status and events, delivered for as long as they last."""

    def __init__(
        self,
        socket_path: Path,
        *,
        on_status: StatusHandler,
        on_event: EventHandler,
        on_connected: ConnectionHandler | None = None,
        first_retry: float = FIRST_RETRY_SECONDS,
        max_retry: float = MAX_RETRY_SECONDS,
    ) -> None:
        self._path = socket_path
        self._on_status = on_status
        self._on_event = on_event
        self._on_connected = on_connected
        self._first_retry = first_retry
        self._max_retry = max_retry

        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self._complained = False

    @property
    def connected(self) -> bool:
        return self._connected

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="anchor-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def run(self) -> None:
        """Connect, follow, reconnect. Returns only when stopped."""
        wait = self._first_retry
        while not self._stopping.is_set():
            if self._follow():
                # We reached the engine, so whatever ended the feed is a fresh
                # outage and gets a fresh backoff. Growing it across a whole
                # login would mean that the fifth package upgrade of the day
                # left the indicator blank for half a minute.
                wait = self._first_retry
            if self._stopping.wait(wait):
                break
            wait = min(wait * 2, self._max_retry)

    # -- one connection --------------------------------------------------

    def _follow(self) -> bool:
        """One connection's worth of following.

        Returns whether the engine was reached at all, which is what decides
        the next backoff. A feed that ran for an hour and then ended because
        the engine restarted is a success followed by an outage, not a failed
        attempt, and treating it as one would make every later reconnection
        slower than the last.
        """
        reached = False
        try:
            with EngineClient(self._path, timeout=READ_TIMEOUT_SECONDS) as client:
                status = client.call("status.get")
                if not status.ok:
                    log.warning("the engine refused status.get: %s", status.error.get("message"))
                    return False

                reached = True
                self._became(connected=True)
                self._deliver_status(status.result)

                for event in client.subscribe(should_stop=self._stopping.is_set):
                    self._deliver_event(event)
        except EngineUnreachableError as error:
            self._became(connected=False, why=str(error))
        except OSError as error:
            self._became(connected=False, why=str(error))
        return reached

    def _became(self, *, connected: bool, why: str = "") -> None:
        if connected:
            if not self._connected:
                log.info("connected to the engine")
            self._complained = False
        elif not self._complained:
            # Once per outage, not once per attempt: an engine that is down
            # for an hour must not fill the journal with the same line.
            log.info("the engine is not reachable, still trying: %s", why)
            self._complained = True

        changed = connected != self._connected
        self._connected = connected
        if changed and self._on_connected is not None:
            try:
                self._on_connected(connected)
            except Exception:
                log.exception("a connection handler raised")

    def _deliver_status(self, status: dict[str, Any]) -> None:
        try:
            self._on_status(status)
        except Exception:
            # A handler that raises must not end the feed: the indicator would
            # then stop updating while the session carried on.
            log.exception("a status handler raised")

    def _deliver_event(self, event: Event) -> None:
        try:
            self._on_event(event)
        except Exception:
            log.exception("an event handler raised for %s", event.event)
