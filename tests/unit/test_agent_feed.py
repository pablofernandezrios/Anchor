"""The corners of the agent's feed, driven by a client that misbehaves."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Self

import pytest

from anchor.agent.feed import EngineFeed
from anchor.cli.client import EngineUnreachableError
from anchor.protocol.messages import Event, Response


class FakeClient:
    """Stands in for a connection, and fails in whichever way a test needs."""

    behaviour: str = "ok"
    events: list[Event] = []  # noqa: RUF012

    def __init__(self, path: Path, *, timeout: float = 0.0) -> None:
        self.path = path

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def call(self, type_: str, payload: dict[str, Any] | None = None) -> Response:
        if self.behaviour == "refused":
            return Response(
                id="1",
                ok=False,
                error={"code": "UNAUTHORIZED", "message": "not your engine"},
            )
        if self.behaviour == "broken_socket":
            raise OSError("the socket went away")
        return Response(id="1", ok=True, result={"active": False})

    def subscribe(self, *args: object, **kwargs: object) -> Iterator[Event]:
        yield from self.events
        raise EngineUnreachableError("the engine closed the event feed")


@pytest.fixture
def feed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EngineFeed:
    monkeypatch.setattr("anchor.agent.feed.EngineClient", FakeClient)
    return EngineFeed(
        tmp_path / "engine.sock",
        on_status=lambda status: None,
        on_event=lambda event: None,
        first_retry=0.01,
        max_retry=0.02,
    )


class TestAnEngineThatAnswersBadly:
    def test_a_refused_status_is_logged_and_retried(
        self, feed: EngineFeed, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Another user's engine, or one that is still starting."""
        monkeypatch.setattr(FakeClient, "behaviour", "refused")

        with caplog.at_level("WARNING", logger="anchor-agent"):
            assert feed._follow() is False  # noqa: SLF001

        assert "not your engine" in caplog.text
        assert not feed.connected

    def test_a_socket_error_is_not_a_crash(
        self, feed: EngineFeed, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """EngineClient raises OSError for anything it did not expect."""
        monkeypatch.setattr(FakeClient, "behaviour", "broken_socket")

        assert feed._follow() is False  # noqa: SLF001
        assert not feed.connected


class TestAConnectionHandlerThatRaises:
    def test_the_feed_survives_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr("anchor.agent.feed.EngineClient", FakeClient)
        monkeypatch.setattr(FakeClient, "behaviour", "ok")

        told: list[bool] = []

        def explode(connected: bool) -> None:
            told.append(connected)
            raise RuntimeError("the tray is on fire")

        feed = EngineFeed(
            tmp_path / "engine.sock",
            on_status=lambda status: None,
            on_event=lambda event: None,
            on_connected=explode,
            first_retry=0.01,
        )

        with caplog.at_level("ERROR", logger="anchor-agent"):
            assert feed._follow() is True  # noqa: SLF001

        assert "connection handler" in caplog.text
        # Both transitions still happened: a handler that threw on the way in
        # must not stop the feed telling it about the way out.
        assert told == [True, False]


class TestTheBackoff:
    def test_it_doubles_and_stops_doubling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A long outage must not turn into a busy loop or an hour of silence."""
        waits: list[float] = []

        monkeypatch.setattr("anchor.agent.feed.EngineClient", FakeClient)
        monkeypatch.setattr(FakeClient, "behaviour", "broken_socket")

        feed = EngineFeed(
            tmp_path / "engine.sock",
            on_status=lambda status: None,
            on_event=lambda event: None,
            first_retry=1.0,
            max_retry=4.0,
        )

        real_wait = feed._stopping.wait  # noqa: SLF001

        def record(timeout: float | None = None) -> bool:
            waits.append(float(timeout or 0))
            return len(waits) >= 5  # stop after five

        monkeypatch.setattr(feed._stopping, "wait", record)  # noqa: SLF001
        feed.run()
        monkeypatch.setattr(feed._stopping, "wait", real_wait)  # noqa: SLF001

        assert waits == [1.0, 2.0, 4.0, 4.0, 4.0]

    def test_a_connection_that_worked_resets_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An engine restarting every hour must not end up checked once a day."""
        waits: list[float] = []
        monkeypatch.setattr("anchor.agent.feed.EngineClient", FakeClient)
        monkeypatch.setattr(FakeClient, "behaviour", "ok")

        feed = EngineFeed(
            tmp_path / "engine.sock",
            on_status=lambda status: None,
            on_event=lambda event: None,
            first_retry=1.0,
            max_retry=8.0,
        )

        def record(timeout: float | None = None) -> bool:
            waits.append(float(timeout or 0))
            return len(waits) >= 3

        monkeypatch.setattr(feed._stopping, "wait", record)  # noqa: SLF001
        feed.run()

        assert waits == [1.0, 1.0, 1.0]
