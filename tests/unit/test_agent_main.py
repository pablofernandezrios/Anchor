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


class FakeOverlay:
    def __init__(self, *, broken: bool = False) -> None:
        self.screens: list[Any] = []
        self.hidden = 0
        self.showing = False
        self.broken = broken

    def show(self, screen: Any) -> None:
        if self.broken:
            raise RuntimeError("the compositor is on fire")
        self.screens.append(screen)
        self.showing = True

    def hide(self) -> None:
        self.hidden += 1
        self.showing = False


def resting(**overrides: Any) -> dict[str, Any]:
    """A status with a break running."""
    rest: dict[str, Any] = {
        "phase": "break",
        "remaining_seconds": 300,
        "ends_at": 0.0,
        "long": False,
        "taken": 0,
        "postponed": 0,
        "skipped": 0,
        "type": "overlay",
        "hardness": "moderate",
        "can_skip": False,
        "can_postpone": True,
        "allow_sites": False,
    }
    rest.update(overrides)
    return status(phase="break", **{"break": rest})


class TestTheBreakOverlay:
    def parts(self, tmp_path: Path) -> tuple[Agent, FakeOverlay]:
        overlay = FakeOverlay()
        agent = Agent(Paths.resolve(tmp_path), tray=FakeTray(), notifier=FakeNotifier())
        agent.use_overlay(overlay)
        return agent, overlay

    def test_it_stays_down_while_working(self, tmp_path: Path) -> None:
        agent, overlay = self.parts(tmp_path)

        agent.on_status(status())

        assert overlay.screens == []

    def test_it_goes_up_when_a_break_starts(self, tmp_path: Path) -> None:
        agent, overlay = self.parts(tmp_path)

        agent.on_status(resting())

        assert overlay.screens[-1].countdown == "05:00"

    def test_it_follows_the_countdown(self, tmp_path: Path) -> None:
        agent, overlay = self.parts(tmp_path)
        agent.on_status(resting())

        agent.on_status(resting(remaining_seconds=299))

        assert overlay.screens[-1].countdown == "04:59"

    def test_it_comes_down_when_the_break_ends(self, tmp_path: Path) -> None:
        agent, overlay = self.parts(tmp_path)
        agent.on_status(resting())

        agent.on_status(status())

        assert overlay.hidden == 1

    def test_a_skipped_break_takes_it_down_at_once(self, tmp_path: Path) -> None:
        """Waiting for the next tick is a second of overlay after a skip."""
        agent, overlay = self.parts(tmp_path)
        agent.on_status(resting())

        agent.on_event(Event(event="break.ended", payload={"skipped": True, "phase": "working"}))

        assert overlay.hidden == 1

    def test_an_overlay_that_raises_does_not_stop_the_agent(self, tmp_path: Path) -> None:
        """A break nobody can see is a worse break, not a broken session."""
        tray = FakeTray()
        agent = Agent(Paths.resolve(tmp_path), tray=tray, notifier=FakeNotifier())
        agent.use_overlay(FakeOverlay(broken=True))

        agent.on_status(resting())

        assert tray.views[-1].visible is True

    def test_with_no_overlay_nothing_happens(self, tmp_path: Path) -> None:
        """A desktop without GTK still gets the notification."""
        notifier = FakeNotifier()
        agent = Agent(Paths.resolve(tmp_path), tray=FakeTray(), notifier=notifier)

        agent.on_status(resting())
        agent.on_event(Event(event="break.started", payload={"seconds": 300, "type": "overlay"}))

        assert notifier.sent[-1].summary == "Break time"


class TestTheIndicatorMenu:
    """The menu ADR 3 made Anchor export for itself (SPEC 14.1)."""

    def agent(self, tmp_path: Path) -> tuple[Any, list[Any]]:
        from anchor.agent.main import Agent
        from anchor.engine.paths import Paths

        shown: list[Any] = []

        class Menu:
            def show(self, items: Any) -> None:
                shown.append(items)

        built = Agent(Paths.resolve(tmp_path))
        built.use_menu(Menu())
        return built, shown

    def test_a_session_puts_its_lines_in_the_menu(self, tmp_path: Path) -> None:
        agent, shown = self.agent(tmp_path)

        agent.on_status(
            {"active": True, "profile": "Study", "level": "firm", "remaining_seconds": 60.0}
        )

        assert shown
        labels = [item.label for item in shown[-1]]
        assert "Extend session" in labels

    def test_no_session_empties_it(self, tmp_path: Path) -> None:
        """An indicator that is not shown has nothing to offer."""
        agent, shown = self.agent(tmp_path)

        agent.on_status({"active": False})

        assert shown[-1] == ()

    def test_open_anchor_starts_the_interface(self, tmp_path: Path) -> None:
        agent, _shown = self.agent(tmp_path)
        opened: list[bool] = []

        import anchor.agent.main as module

        original = module.open_the_interface
        module.open_the_interface = lambda: opened.append(True)  # type: ignore[assignment]
        try:
            agent.on_menu_action("open")
        finally:
            module.open_the_interface = original  # type: ignore[assignment]

        assert opened == [True]

    def test_extending_opens_it_too(self, tmp_path: Path) -> None:
        """The menu has nowhere to ask how long, and guessing is not Anchor's."""
        agent, _shown = self.agent(tmp_path)
        opened: list[bool] = []

        import anchor.agent.main as module

        original = module.open_the_interface
        module.open_the_interface = lambda: opened.append(True)  # type: ignore[assignment]
        try:
            agent.on_menu_action("extend")
        finally:
            module.open_the_interface = original  # type: ignore[assignment]

        assert opened == [True]

    def test_an_action_nothing_is_bound_to_is_ignored(self, tmp_path: Path) -> None:
        agent, _shown = self.agent(tmp_path)
        agent.on_menu_action("fly")
