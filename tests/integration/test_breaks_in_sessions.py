"""Breaks in a running session, over the socket (SPEC 10).

The pattern itself is tested in the unit suite. This is about the parts that
only exist once a break is inside a session: what the blocker is told, what
clients can see, and what happens when someone asks to skip one.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from anchor.cli.client import EngineClient
from anchor.engine.core import Engine
from anchor.engine.paths import Paths, Settings
from anchor.engine.profiles import BreakSettings, Profile
from anchor.engine.service import EngineServer
from anchor.engine.timekeeping import FakeClock
from anchor.protocol.errors import ErrorCode
from anchor.protocol.messages import Event
from anchor.protocol.types import BreakHardness, SessionPhase, WebMode

HOUR = 3600
MINUTE = 60


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    tree = Paths.resolve(tmp_path)
    tree.ensure_directories()
    return tree


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(wall_time=1_760_000_000.0, boottime=1_000.0, boot="boot-a")


@pytest.fixture
def events() -> list[Event]:
    return []


@pytest.fixture
def engine(paths: Paths, clock: FakeClock, tmp_path: Path, events: list[Event]) -> Engine:
    engine = Engine(
        paths,
        Settings(owner_uid=os.getuid()),
        clock=clock,
        systemd_runtime_dir=tmp_path / "run" / "systemd" / "system",
    )
    engine.load()
    engine.profiles["Study"] = Profile(
        name="Study",
        web_mode=WebMode.BLOCKLIST,
        domains=frozenset({"youtube.com"}),
        apps=frozenset({"discord.desktop"}),
        breaks=BreakSettings(work_minutes=25, break_minutes=5, warning_seconds=60),
    )
    engine.save_config()
    engine.subscribe(events.append)
    return engine


@pytest.fixture
def client(engine: Engine, paths: Paths) -> Iterator[EngineClient]:
    server = EngineServer(engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with EngineClient(paths.engine_socket, timeout=5.0) as client:
            yield client
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def start(client: EngineClient) -> None:
    response = client.call(
        "session.start",
        {"profile": "Study", "duration_seconds": 4 * HOUR, "level": "firm"},
    )
    assert response.ok, response.error


def status(client: EngineClient) -> dict[str, Any]:
    return client.call("status.get").result


def policy(client: EngineClient) -> dict[str, Any]:
    return client.call("policy.get").result


def named(events: list[Event], name: str) -> list[Event]:
    return [event for event in events if event.event == name]


def tick_through(
    client: EngineClient, clock: FakeClock, seconds: float, step: float = 30.0
) -> None:
    """Let time pass the way it passes for a running engine.

    Jumping the whole way in one go is not the same thing: an hour that
    arrives between two ticks is an absence, and SPEC 10 treats it as one.
    """
    passed = 0.0
    while passed < seconds:
        clock.advance(step)
        passed += step
        client.call("status.get")


class TestWhatAClientCanSee:
    def test_a_session_knows_when_its_next_break_is(
        self, client: EngineClient, clock: FakeClock
    ) -> None:
        """SPEC 14.1: the indicator's menu shows it."""
        start(client)

        rest = status(client)["break"]
        assert rest["phase"] == str(SessionPhase.WORKING)
        assert rest["remaining_seconds"] == pytest.approx(25 * MINUTE)

    def test_it_also_knows_how_long_that_break_will_be(
        self, client: EngineClient, clock: FakeClock
    ) -> None:
        """Home draws "10 min · fullscreen" for the break that is coming."""
        start(client)

        assert status(client)["break"]["break_seconds"] == 5 * MINUTE

    def test_and_how_long_the_one_running_is(self, client: EngineClient, clock: FakeClock) -> None:
        start(client)
        clock.advance(25 * MINUTE)

        assert status(client)["break"]["break_seconds"] == 5 * MINUTE

    def test_the_phase_changes_when_the_break_starts(
        self, client: EngineClient, clock: FakeClock
    ) -> None:
        start(client)
        clock.advance(25 * MINUTE)

        now = status(client)
        assert now["phase"] == str(SessionPhase.BREAK)
        assert now["break"]["remaining_seconds"] == pytest.approx(5 * MINUTE)

    def test_the_counters_are_visible(self, client: EngineClient, clock: FakeClock) -> None:
        start(client)
        tick_through(client, clock, 31 * MINUTE)

        assert status(client)["break"]["taken"] == 1

    def test_a_session_without_a_pattern_says_so_rather_than_lying(
        self, client: EngineClient, engine: Engine
    ) -> None:
        """A profile can be uninstalled while its session runs."""
        start(client)
        del engine.profiles["Study"]

        assert status(client)["break"]["type"] is None


class TestWhatTheEnginePublishes:
    def test_the_warning_comes_before_the_break(
        self, client: EngineClient, clock: FakeClock, events: list[Event]
    ) -> None:
        start(client)
        clock.advance(24 * MINUTE + 30)
        status(client)

        assert named(events, "break.warning")

    def test_the_start_carries_what_the_overlay_needs(
        self, client: EngineClient, clock: FakeClock, events: list[Event]
    ) -> None:
        start(client)
        clock.advance(25 * MINUTE)
        status(client)

        started = named(events, "break.started")
        assert started, [event.event for event in events]
        assert started[0].payload["seconds"] == 5 * MINUTE
        assert started[0].payload["type"] == "overlay"

    def test_the_end_is_published_too(
        self, client: EngineClient, clock: FakeClock, events: list[Event]
    ) -> None:
        start(client)
        tick_through(client, clock, 31 * MINUTE)

        assert named(events, "break.ended")

    def test_an_absence_that_covers_the_break_publishes_nothing(
        self, client: EngineClient, clock: FakeClock, events: list[Event]
    ) -> None:
        """SPEC 10: it counts as taken. An overlay for it would be a lie.

        This is the same half-hour as the test above, arriving in one jump
        instead of in ticks, which is what a suspended laptop looks like.
        """
        start(client)
        clock.advance(31 * MINUTE)
        status(client)

        assert named(events, "break.started") == []
        assert named(events, "break.ended") == []
        assert status(client)["break"]["taken"] == 1


class TestWhatTheBlockerIsTold:
    def test_a_break_does_not_unblock_anything_by_default(
        self, client: EngineClient, clock: FakeClock
    ) -> None:
        """SPEC 10 says so outright."""
        start(client)
        clock.advance(25 * MINUTE)

        during = policy(client)
        assert during["domains"] == ["youtube.com"]
        assert during["apps"] == ["discord.desktop"]

    def test_a_profile_can_allow_sites_during_breaks(
        self, client: EngineClient, clock: FakeClock, engine: Engine
    ) -> None:
        engine.profiles["Study"] = Profile(
            name="Study",
            domains=frozenset({"youtube.com"}),
            apps=frozenset({"discord.desktop"}),
            breaks=BreakSettings(work_minutes=25, break_minutes=5, allow_sites_during_breaks=True),
        )
        engine.save_config()
        start(client)
        clock.advance(25 * MINUTE)

        during = policy(client)
        assert during["domains"] == []

    def test_but_never_the_applications(
        self, client: EngineClient, clock: FakeClock, engine: Engine
    ) -> None:
        """Five minutes is long enough to lose an hour in a game."""
        engine.profiles["Study"] = Profile(
            name="Study",
            domains=frozenset({"youtube.com"}),
            apps=frozenset({"discord.desktop"}),
            breaks=BreakSettings(work_minutes=25, break_minutes=5, allow_sites_during_breaks=True),
        )
        engine.save_config()
        start(client)
        clock.advance(25 * MINUTE)

        assert policy(client)["apps"] == ["discord.desktop"]

    def test_work_resumes_with_everything_blocked_again(
        self, client: EngineClient, clock: FakeClock, engine: Engine
    ) -> None:
        engine.profiles["Study"] = Profile(
            name="Study",
            domains=frozenset({"youtube.com"}),
            breaks=BreakSettings(work_minutes=25, break_minutes=5, allow_sites_during_breaks=True),
        )
        engine.save_config()
        start(client)
        tick_through(client, clock, 31 * MINUTE)

        assert policy(client)["domains"] == ["youtube.com"]


class TestSkippingAndPostponing:
    def flexible(self, engine: Engine, hardness: BreakHardness) -> None:
        engine.profiles["Study"] = Profile(
            name="Study",
            domains=frozenset({"youtube.com"}),
            breaks=BreakSettings(work_minutes=25, break_minutes=5, hardness=hardness),
        )
        engine.save_config()

    def test_a_flexible_break_can_be_skipped(
        self, client: EngineClient, clock: FakeClock, engine: Engine
    ) -> None:
        self.flexible(engine, BreakHardness.FLEXIBLE)
        start(client)
        clock.advance(25 * MINUTE)

        response = client.call("break.skip")

        assert response.ok, response.error
        assert response.result["phase"] == str(SessionPhase.WORKING)
        assert response.result["break"]["skipped"] == 1

    def test_a_mandatory_break_cannot(
        self, client: EngineClient, clock: FakeClock, engine: Engine
    ) -> None:
        self.flexible(engine, BreakHardness.MANDATORY)
        start(client)
        clock.advance(25 * MINUTE)

        response = client.call("break.skip")

        assert not response.ok
        assert response.error["code"] == ErrorCode.SKIP_FORBIDDEN

    def test_a_moderate_break_can_be_postponed_once(
        self, client: EngineClient, clock: FakeClock, engine: Engine
    ) -> None:
        self.flexible(engine, BreakHardness.MODERATE)
        start(client)
        clock.advance(25 * MINUTE)

        assert client.call("break.postpone").ok
        clock.advance(6 * MINUTE)
        assert status(client)["phase"] == str(SessionPhase.BREAK)

        assert not client.call("break.postpone").ok

    def test_skipping_when_nothing_is_running_is_refused(self, client: EngineClient) -> None:
        response = client.call("break.skip")

        assert not response.ok
        assert response.error["code"] == ErrorCode.NO_ACTIVE_SESSION

    def test_skipping_without_a_break_is_refused_clearly(
        self, client: EngineClient, engine: Engine
    ) -> None:
        self.flexible(engine, BreakHardness.FLEXIBLE)
        start(client)

        response = client.call("break.skip")

        assert not response.ok
        assert "no break" in response.error["message"]


class TestAcrossARestart:
    def test_the_cycle_survives_the_engine_restarting(
        self, client: EngineClient, clock: FakeClock, engine: Engine, paths: Paths
    ) -> None:
        """A package upgrade mid-session must not reset the pattern."""
        start(client)
        clock.advance(20 * MINUTE)
        status(client)

        revived = Engine(paths, Settings(owner_uid=os.getuid()), clock=clock)
        revived.load()

        session = revived.state.session
        assert session is not None
        assert session.breaks is not None
        assert session.breaks.remaining(clock) == pytest.approx(5 * MINUTE, abs=2)


class TestTheCommandLine:
    """SPEC 15 asks for parity with the interface, and the overlay has buttons."""

    def cli(self, paths: Paths, *arguments: str) -> tuple[int, str, str]:
        import subprocess
        import sys

        done = subprocess.run(
            [
                sys.executable,
                "-m",
                "anchor.cli.main",
                "--root",
                str(paths.state_dir.parents[2]),
                *arguments,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return done.returncode, done.stdout, done.stderr

    def flexible(self, engine: Engine) -> None:
        engine.profiles["Study"] = Profile(
            name="Study",
            domains=frozenset({"youtube.com"}),
            breaks=BreakSettings(work_minutes=25, break_minutes=5, hardness=BreakHardness.FLEXIBLE),
        )
        engine.save_config()

    def test_a_break_can_be_skipped_from_the_terminal(
        self, client: EngineClient, clock: FakeClock, engine: Engine, paths: Paths
    ) -> None:
        self.flexible(engine)
        start(client)
        clock.advance(25 * MINUTE)
        assert status(client)["phase"] == str(SessionPhase.BREAK)

        code, out, err = self.cli(paths, "break", "skip")

        assert code == 0, err
        assert status(client)["phase"] == str(SessionPhase.WORKING)

    def test_and_postponed(
        self, client: EngineClient, clock: FakeClock, engine: Engine, paths: Paths
    ) -> None:
        self.flexible(engine)
        start(client)
        clock.advance(25 * MINUTE)

        code, _out, err = self.cli(paths, "break", "postpone")

        assert code == 0, err
        assert status(client)["break"]["postponed"] == 1

    def test_a_refusal_carries_the_exit_code_for_one(
        self, client: EngineClient, clock: FakeClock, engine: Engine, paths: Paths
    ) -> None:
        """SPEC 15: refused on purpose is exit code 4, and scripts rely on it."""
        engine.profiles["Study"] = Profile(
            name="Study",
            breaks=BreakSettings(
                work_minutes=25, break_minutes=5, hardness=BreakHardness.MANDATORY
            ),
        )
        engine.save_config()
        start(client)
        clock.advance(25 * MINUTE)

        code, _out, err = self.cli(paths, "break", "skip")

        assert code == 4
        assert "cannot be skipped" in err

    def test_with_no_break_running_it_says_so(
        self, client: EngineClient, engine: Engine, paths: Paths
    ) -> None:
        self.flexible(engine)
        start(client)

        code, _out, err = self.cli(paths, "break", "skip")

        assert code != 0
        assert "no break" in err
