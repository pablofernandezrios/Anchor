"""The agent's wiring: engine events in, desktop calls out (SPEC 5.1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from anchor.agent.indicator import IndicatorView
from anchor.agent.main import Agent, _parse_args
from anchor.agent.notifications import Notification
from anchor.engine.paths import Paths
from anchor.protocol.messages import Event

HOUR = 3600


class FakeTray:
    def __init__(self, *, broken: bool = False) -> None:
        self.views: list[IndicatorView] = []
        self.broken = broken

    def show(self, view: IndicatorView) -> None:
        if self.broken:
            raise RuntimeError("the panel is on fire")
        self.views.append(view)


class FakeNotifier:
    def __init__(self, *, broken: bool = False) -> None:
        self.sent: list[Notification] = []
        self.broken = broken

    def send(self, notification: Notification) -> None:
        if self.broken:
            raise RuntimeError("the notification server is on fire")
        self.sent.append(notification)


def status(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "active": True,
        "profile": "Study",
        "level": "firm",
        "phase": "working",
        "ends_at": 1_760_000_000.0 + HOUR,
        "remaining_seconds": HOUR,
        "blocked_attempts": 0,
        "app_blocks": 0,
    }
    base.update(overrides)
    return base


@pytest.fixture
def parts(tmp_path: Path) -> tuple[Agent, FakeTray, FakeNotifier]:
    tray, notifier = FakeTray(), FakeNotifier()
    agent = Agent(Paths.resolve(tmp_path), tray=tray, notifier=notifier)
    return agent, tray, notifier


class TestDrawing:
    def test_a_status_reaches_the_panel(self, parts: tuple[Agent, FakeTray, FakeNotifier]) -> None:
        agent, tray, _ = parts

        agent.on_status(status())

        assert tray.views[-1].visible is True
        assert tray.views[-1].label == "1:00"

    def test_a_tick_redraws_it(self, parts: tuple[Agent, FakeTray, FakeNotifier]) -> None:
        agent, tray, _ = parts
        agent.on_status(status())

        agent.on_event(Event(event="session.tick", payload=status(remaining_seconds=1800)))

        assert tray.views[-1].label == "0:30"

    def test_losing_the_engine_redraws_it(
        self, parts: tuple[Agent, FakeTray, FakeNotifier]
    ) -> None:
        agent, tray, _ = parts
        agent.on_status(status())

        agent.on_connected(False)

        assert tray.views[-1].label == "—"

    def test_a_panel_that_raises_does_not_stop_the_agent(self, tmp_path: Path) -> None:
        """Anchor keeps blocking with or without a top bar."""
        notifier = FakeNotifier()
        agent = Agent(Paths.resolve(tmp_path), tray=FakeTray(broken=True), notifier=notifier)

        agent.on_status(status())
        agent.on_event(Event(event="blocked.attempt", payload={"domain": "a.com", "rule": "a.com"}))

        assert len(notifier.sent) == 1


class TestSaying:
    def test_a_blocked_site_is_announced(self, parts: tuple[Agent, FakeTray, FakeNotifier]) -> None:
        agent, _, notifier = parts

        agent.on_event(
            Event(event="blocked.attempt", payload={"domain": "youtube.com", "rule": "youtube.com"})
        )

        assert notifier.sent[-1].summary == "Site blocked"

    def test_the_grace_warning_is_announced(
        self, parts: tuple[Agent, FakeTray, FakeNotifier]
    ) -> None:
        agent, _, notifier = parts

        agent.on_event(Event(event="apps.grace", payload={"apps": ["Discord"], "seconds": 120}))

        assert "will close in 2 minutes" in notifier.sent[-1].summary

    def test_a_tick_says_nothing(self, parts: tuple[Agent, FakeTray, FakeNotifier]) -> None:
        """Once a second, every second, would be unusable."""
        agent, _, notifier = parts

        agent.on_event(Event(event="session.tick", payload=status()))

        assert notifier.sent == []

    def test_a_notifier_that_raises_does_not_stop_the_agent(self, tmp_path: Path) -> None:
        tray = FakeTray()
        agent = Agent(Paths.resolve(tmp_path), tray=tray, notifier=FakeNotifier(broken=True))

        agent.on_event(Event(event="apps.closed", payload={"apps": ["Discord"]}))
        agent.on_status(status())

        assert tray.views[-1].visible is True


class TestWithNoDesktop:
    def test_it_still_follows_along(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """--dry-run, and the fallback when there is no session bus."""
        agent = Agent(Paths.resolve(tmp_path))

        with caplog.at_level("INFO", logger="anchor-agent"):
            agent.on_status(status())
            agent.on_event(Event(event="apps.closed", payload={"apps": ["Discord"]}))

        assert "Anchor closed Discord" in caplog.text


class TestScheduling:
    def test_drawing_is_handed_to_the_desktop_thread(self, tmp_path: Path) -> None:
        """D-Bus is not where to find out what happens when threads meet."""
        scheduled: list[Any] = []
        tray = FakeTray()
        agent = Agent(
            Paths.resolve(tmp_path),
            tray=tray,
            notifier=FakeNotifier(),
            schedule=scheduled.append,
        )

        agent.on_status(status())

        assert tray.views == [], "the panel was touched from the feed's thread"
        assert len(scheduled) == 1

        scheduled[0]()
        assert tray.views[-1].visible is True

    def test_so_are_notifications(self, tmp_path: Path) -> None:
        scheduled: list[Any] = []
        notifier = FakeNotifier()
        agent = Agent(
            Paths.resolve(tmp_path),
            tray=FakeTray(),
            notifier=notifier,
            schedule=scheduled.append,
        )

        agent.on_event(Event(event="apps.closed", payload={"apps": ["Discord"]}))

        assert notifier.sent == []
        for work in scheduled:
            work()
        assert len(notifier.sent) == 1


class TestTheCommandLine:
    def test_it_takes_a_relocated_tree(self) -> None:
        assert _parse_args(["--root", "/tmp/anchor"]).root == "/tmp/anchor"

    def test_dry_run_is_off_by_default(self) -> None:
        assert _parse_args([]).dry_run is False
