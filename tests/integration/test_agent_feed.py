"""The agent following a real engine over its socket (SPEC 5.1, P4).

The interesting cases are all about the engine going away: it restarts on a
package upgrade, and the agent has to come back on its own. An indicator that
vanished when the engine restarted would look exactly like a session that
ended, which is the one lie Anchor must not tell.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from anchor.agent.feed import READ_TIMEOUT_SECONDS, EngineFeed
from anchor.blocker.watcher import wait_for
from anchor.engine.core import Engine
from anchor.engine.paths import Paths, Settings
from anchor.engine.profiles import Profile
from anchor.engine.service import EngineServer
from anchor.engine.timekeeping import FakeClock
from anchor.protocol.messages import Event

HOUR = 3600


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    tree = Paths.resolve(tmp_path)
    tree.ensure_directories()
    return tree


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(wall_time=1_760_000_000.0, boottime=1_000.0, boot="boot-a")


@pytest.fixture
def engine(paths: Paths, clock: FakeClock, tmp_path: Path) -> Engine:
    engine = Engine(
        paths,
        Settings(owner_uid=os.getuid()),
        clock=clock,
        systemd_runtime_dir=tmp_path / "run" / "systemd" / "system",
    )
    engine.load()
    engine.profiles["Study"] = Profile(name="Study", domains=frozenset({"youtube.com"}))
    engine.save_config()
    return engine


class Server:
    """An engine on its socket, which a test can stop and start again."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._server: EngineServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._server = EngineServer(self.engine)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def emit(self, event: str, payload: dict[str, Any] | None = None) -> None:
        self.engine.emit(event, payload or {})


@pytest.fixture
def server(engine: Engine) -> Iterator[Server]:
    server = Server(engine)
    server.start()
    try:
        yield server
    finally:
        server.stop()


class Watcher:
    """Everything the agent was told."""

    def __init__(self) -> None:
        self.statuses: list[dict[str, Any]] = []
        self.events: list[Event] = []
        self.connections: list[bool] = []
        self.lock = threading.Lock()

    def on_status(self, status: dict[str, Any]) -> None:
        with self.lock:
            self.statuses.append(status)

    def on_event(self, event: Event) -> None:
        with self.lock:
            self.events.append(event)

    def on_connected(self, connected: bool) -> None:
        with self.lock:
            self.connections.append(connected)

    def named(self, name: str) -> list[Event]:
        with self.lock:
            return [event for event in self.events if event.event == name]


@pytest.fixture
def watcher() -> Watcher:
    return Watcher()


@pytest.fixture
def feed(paths: Paths, watcher: Watcher) -> Iterator[EngineFeed]:
    feed = EngineFeed(
        paths.engine_socket,
        on_status=watcher.on_status,
        on_event=watcher.on_event,
        on_connected=watcher.on_connected,
        first_retry=0.05,
        max_retry=0.2,
    )
    try:
        yield feed
    finally:
        feed.stop()


def start_session(engine: Engine) -> None:
    from anchor.protocol.messages import Request

    engine.handle(
        Request(
            type="session.start",
            payload={
                "profile": "Study",
                "duration_seconds": HOUR,
                "level": "soft",
                "origin": "manual",
                "valve": None,
            },
        )
    )


class TestFollowingTheEngine:
    def test_the_status_arrives_without_waiting_for_an_event(
        self, server: Server, feed: EngineFeed, watcher: Watcher
    ) -> None:
        """With no session running the engine sends nothing at all."""
        feed.start()

        assert wait_for(lambda: bool(watcher.statuses), timeout=5)
        assert watcher.statuses[0]["active"] is False

    def test_events_arrive_as_they_are_published(
        self, server: Server, feed: EngineFeed, watcher: Watcher
    ) -> None:
        feed.start()
        assert wait_for(lambda: feed.connected, timeout=5)

        server.emit("blocked.attempt", {"domain": "youtube.com", "rule": "youtube.com"})

        assert wait_for(lambda: bool(watcher.named("blocked.attempt")), timeout=5)
        assert watcher.named("blocked.attempt")[0].payload["domain"] == "youtube.com"

    def test_a_running_session_is_reported_on_connection(
        self, server: Server, feed: EngineFeed, watcher: Watcher, engine: Engine
    ) -> None:
        """An agent that starts mid-session must show the session, not nothing."""
        start_session(engine)
        feed.start()

        assert wait_for(lambda: bool(watcher.statuses), timeout=5)
        assert watcher.statuses[0]["active"] is True
        assert watcher.statuses[0]["profile"] == "Study"


class TestAQuietEngine:
    """Nothing happening is the normal case, and it used to end the feed.

    Found by running the agent on a real desktop: it logged "connected to the
    engine" and "the engine is not reachable" alternately, twice a second,
    for as long as it ran. Anchor was idle, which is when it should be
    silent.

    Catching the read timeout was not enough. Python's socket file object
    records that a read timed out and refuses every later read with
    "cannot read from timed out object" -- an OSError, so the feed read it as
    a broken connection and reconnected, over and over.
    """

    def test_the_feed_survives_far_longer_than_one_read_timeout(
        self, server: Server, feed: EngineFeed, watcher: Watcher, engine: Engine
    ) -> None:
        feed.start()
        assert wait_for(lambda: watcher.connections == [True], timeout=5)

        # Long enough for several reads to time out on an idle feed.
        time.sleep(READ_TIMEOUT_SECONDS * 2.5)

        # Still one connection, never dropped: [True, False, True, ...] is the
        # loop this test exists for.
        assert watcher.connections == [True], "the feed reconnected while nothing happened"

    def test_and_an_event_after_the_quiet_still_arrives(
        self, server: Server, feed: EngineFeed, watcher: Watcher, engine: Engine
    ) -> None:
        """The feed must be reading, not merely unbroken."""
        feed.start()
        assert wait_for(lambda: watcher.connections == [True], timeout=5)

        time.sleep(READ_TIMEOUT_SECONDS * 1.5)
        start_session(engine)

        assert wait_for(
            lambda: len(watcher.events) > 0, timeout=5
        ), "the feed stopped reading after a quiet moment"


class TestWhenTheEngineGoesAway:
    def test_it_waits_rather_than_giving_up(
        self, paths: Paths, feed: EngineFeed, watcher: Watcher, engine: Engine
    ) -> None:
        """The agent starts with the user session; the engine may be later."""
        feed.start()

        server = Server(engine)
        server.start()
        try:
            assert wait_for(lambda: bool(watcher.statuses), timeout=10)
        finally:
            server.stop()

    def test_it_reconnects_after_a_restart(
        self, server: Server, feed: EngineFeed, watcher: Watcher
    ) -> None:
        feed.start()
        assert wait_for(lambda: feed.connected, timeout=5)

        server.stop()
        assert wait_for(lambda: not feed.connected, timeout=5)
        server.start()

        assert wait_for(lambda: feed.connected, timeout=10)
        assert len(watcher.statuses) >= 2, "the status was not asked for again"

    def test_the_loss_and_the_return_are_both_reported(
        self, server: Server, feed: EngineFeed, watcher: Watcher
    ) -> None:
        """The indicator needs to know, so it can say it does not know."""
        feed.start()
        assert wait_for(lambda: feed.connected, timeout=5)

        server.stop()
        assert wait_for(lambda: watcher.connections[-1] is False, timeout=5)
        server.start()
        assert wait_for(lambda: watcher.connections[-1] is True, timeout=10)

        assert watcher.connections[:3] == [True, False, True]

    def test_events_flow_again_afterwards(
        self, server: Server, feed: EngineFeed, watcher: Watcher
    ) -> None:
        feed.start()
        assert wait_for(lambda: feed.connected, timeout=5)
        server.stop()
        server.start()
        assert wait_for(lambda: feed.connected, timeout=10)

        server.emit("session.ended", {"reason": "completed"})

        assert wait_for(lambda: bool(watcher.named("session.ended")), timeout=5)

    def test_the_outage_is_logged_once_rather_than_per_attempt(
        self, feed: EngineFeed, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An engine down for an hour must not fill the journal."""
        with caplog.at_level("INFO", logger="anchor-agent"):
            feed.start()
            wait_for(lambda: False, timeout=0.6)  # several retries go by
            feed.stop()

        complaints = [line for line in caplog.text.splitlines() if "not reachable" in line]
        assert len(complaints) == 1, complaints


class TestHandlersThatMisbehave:
    def test_a_raising_status_handler_does_not_end_the_feed(
        self, server: Server, paths: Paths, watcher: Watcher
    ) -> None:
        def explode(status: dict[str, Any]) -> None:
            raise RuntimeError("the indicator is on fire")

        feed = EngineFeed(
            paths.engine_socket,
            on_status=explode,
            on_event=watcher.on_event,
            first_retry=0.05,
        )
        feed.start()
        try:
            assert wait_for(lambda: feed.connected, timeout=5)
            server.emit("session.ended", {"reason": "completed"})
            assert wait_for(lambda: bool(watcher.named("session.ended")), timeout=5)
        finally:
            feed.stop()

    def test_a_raising_event_handler_does_not_end_the_feed(
        self, server: Server, paths: Paths, watcher: Watcher
    ) -> None:
        seen: list[str] = []

        def explode(event: Event) -> None:
            seen.append(event.event)
            raise RuntimeError("the notification server is on fire")

        feed = EngineFeed(
            paths.engine_socket,
            on_status=watcher.on_status,
            on_event=explode,
            first_retry=0.05,
        )
        feed.start()
        try:
            assert wait_for(lambda: feed.connected, timeout=5)
            server.emit("session.ended", {"reason": "completed"})
            assert wait_for(lambda: len(seen) >= 1, timeout=5)
            server.emit("session.started", {"session_id": "x"})
            assert wait_for(lambda: len(seen) >= 2, timeout=5)
        finally:
            feed.stop()


class TestStopping:
    def test_stopping_ends_the_thread(self, server: Server, feed: EngineFeed) -> None:
        feed.start()
        assert wait_for(lambda: feed.connected, timeout=5)

        feed.stop()

        assert feed._thread is None  # noqa: SLF001

    def test_stopping_before_starting_is_harmless(self, feed: EngineFeed) -> None:
        feed.stop()
