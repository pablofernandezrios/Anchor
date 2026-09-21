"""Schedules that actually start and end sessions (SPEC 11).

The model and the merging are tested on their own. This is the engine using
them: a window opening, a late boot joining one in progress, an overlap, a
skip, and the rules that stop a running schedule being edited out from under
the session it started.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from anchor.engine.core import Engine
from anchor.engine.paths import Paths, Settings
from anchor.engine.profiles import Profile
from anchor.engine.schedules import Schedule
from anchor.engine.timekeeping import FakeClock
from anchor.protocol.errors import ErrorCode
from anchor.protocol.messages import Request
from anchor.protocol.types import Level, SessionOrigin, Valve

WEDNESDAY = date(2026, 9, 23)
HOUR = 3600


def at(day: date, hour: int, minute: int = 0) -> float:
    return datetime(day.year, day.month, day.day, hour, minute).timestamp()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(wall_time=at(WEDNESDAY, 8), boottime=1_000.0, boot="boot-a")


@pytest.fixture
def engine(tmp_path: Path, clock: FakeClock) -> Engine:
    paths = Paths.resolve(tmp_path)
    paths.ensure_directories()
    engine = Engine(
        paths,
        Settings(owner_uid=os.getuid()),
        clock=clock,
        systemd_runtime_dir=tmp_path / "run" / "systemd" / "system",
    )
    engine.load()
    engine.profiles["Study"] = Profile(name="Study", domains=frozenset({"youtube.com"}))
    engine.profiles["Work"] = Profile(name="Work", domains=frozenset({"reddit.com"}))
    engine.save_config()
    return engine


def a_schedule(engine: Engine, **overrides: Any) -> Schedule:
    base: dict[str, Any] = {
        "id": overrides.pop("id", "s1"),
        "name": "Mornings",
        "profile": "Study",
        "days": frozenset({2}),  # Wednesday
        "start_minute": 9 * 60,
        "end_minute": 13 * 60,
        "level": Level.FIRM,
    }
    base.update(overrides)
    schedule = Schedule(**base)
    engine.schedules[schedule.id] = schedule
    engine.save_config()
    return schedule


class TestAWindowOpening:
    def test_nothing_runs_before_it(self, engine: Engine) -> None:
        a_schedule(engine)

        engine.tick()

        assert engine.state.session is None

    def test_the_session_starts_when_it_opens(self, engine: Engine, clock: FakeClock) -> None:
        a_schedule(engine)
        clock.advance(HOUR)  # 09:00

        engine.tick()

        session = engine.state.session
        assert session is not None
        assert session.origin is SessionOrigin.SCHEDULE
        assert session.level is Level.FIRM

    def test_it_ends_when_the_window_does(self, engine: Engine, clock: FakeClock) -> None:
        a_schedule(engine)
        clock.advance(HOUR)
        engine.tick()

        clock.advance(4 * HOUR + 60)
        engine.tick()

        assert engine.state.session is None

    def test_a_disabled_schedule_starts_nothing(self, engine: Engine, clock: FakeClock) -> None:
        a_schedule(engine, enabled=False)
        clock.advance(HOUR)

        engine.tick()

        assert engine.state.session is None

    def test_the_eight_hour_cap_does_not_apply(self, engine: Engine, clock: FakeClock) -> None:
        """SPEC 11 says so outright: the cap is for sessions started by hand."""
        a_schedule(engine, start_minute=9 * 60, end_minute=23 * 60)
        clock.advance(HOUR)

        engine.tick()

        session = engine.state.session
        assert session is not None
        assert session.anchor.duration_seconds == pytest.approx(14 * HOUR)


class TestALateBoot:
    def test_the_session_starts_with_the_time_that_is_left(
        self, engine: Engine, clock: FakeClock
    ) -> None:
        """SPEC 11: if the machine boots late, it joins the window."""
        a_schedule(engine)
        clock.advance(3 * HOUR)  # 11:00, two hours into a 09:00-13:00 window

        engine.tick()

        session = engine.state.session
        assert session is not None
        assert session.anchor.duration_seconds == pytest.approx(2 * HOUR)

    def test_nothing_is_owed_for_the_part_that_was_missed(
        self, engine: Engine, clock: FakeClock
    ) -> None:
        a_schedule(engine)
        clock.advance(3 * HOUR)
        engine.tick()

        clock.advance(2 * HOUR + 60)
        engine.tick()

        assert engine.state.session is None

    def test_booting_after_the_window_starts_nothing(
        self, engine: Engine, clock: FakeClock
    ) -> None:
        a_schedule(engine)
        clock.advance(6 * HOUR)  # 14:00

        engine.tick()

        assert engine.state.session is None


class TestOverlaps:
    def test_the_strictest_level_wins(self, engine: Engine, clock: FakeClock) -> None:
        a_schedule(engine, level=Level.SOFT)
        a_schedule(
            engine,
            id="s2",
            name="Afternoon",
            profile="Work",
            level=Level.STRICT,
            valve=Valve.WAIT,
            start_minute=12 * 60,
            end_minute=16 * 60,
        )
        clock.advance(4 * HOUR)  # 12:00, both running

        engine.tick()

        session = engine.state.session
        assert session is not None
        assert session.level is Level.STRICT
        assert session.valve is Valve.WAIT

    def test_the_blocker_is_told_about_both(self, engine: Engine, clock: FakeClock) -> None:
        a_schedule(engine)
        a_schedule(
            engine,
            id="s2",
            name="Afternoon",
            profile="Work",
            start_minute=12 * 60,
            end_minute=16 * 60,
        )
        clock.advance(4 * HOUR)
        engine.tick()

        policy = engine.handle(Request(type="policy.get", payload={})).result

        assert set(policy["domains"]) == {"youtube.com", "reddit.com"}

    def test_the_session_lasts_until_the_last_one_ends(
        self, engine: Engine, clock: FakeClock
    ) -> None:
        a_schedule(engine)
        a_schedule(
            engine,
            id="s2",
            name="Afternoon",
            profile="Work",
            start_minute=12 * 60,
            end_minute=16 * 60,
        )
        clock.advance(4 * HOUR)
        engine.tick()

        clock.advance(HOUR + 60)  # 13:01, the first schedule has ended
        engine.tick()

        assert engine.state.session is not None

    def test_and_ends_when_it_does(self, engine: Engine, clock: FakeClock) -> None:
        a_schedule(engine)
        a_schedule(
            engine,
            id="s2",
            name="Afternoon",
            profile="Work",
            start_minute=12 * 60,
            end_minute=16 * 60,
        )
        clock.advance(4 * HOUR)
        engine.tick()

        clock.advance(4 * HOUR + 60)  # 16:01
        engine.tick()

        assert engine.state.session is None


class TestAManualSessionInTheWay:
    def test_a_schedule_does_not_start_a_second_session(
        self, engine: Engine, clock: FakeClock
    ) -> None:
        engine.handle(
            Request(
                type="session.start",
                payload={
                    "profile": "Study",
                    "duration_seconds": 6 * HOUR,
                    "level": "soft",
                    "origin": "manual",
                    "valve": None,
                },
            )
        )
        a_schedule(engine)
        clock.advance(HOUR)

        engine.tick()

        session = engine.state.session
        assert session is not None
        assert session.origin == SessionOrigin.MANUAL

    def test_and_starts_one_when_the_manual_session_ends(
        self, engine: Engine, clock: FakeClock
    ) -> None:
        engine.handle(
            Request(
                type="session.start",
                payload={
                    "profile": "Study",
                    "duration_seconds": 2 * HOUR,
                    "level": "soft",
                    "origin": "manual",
                    "valve": None,
                },
            )
        )
        a_schedule(engine)
        clock.advance(2 * HOUR + 60)  # the manual session is over; 10:01

        engine.tick()

        session = engine.state.session
        assert session is not None
        assert session.origin == SessionOrigin.SCHEDULE


class TestSkipping:
    def running(self, engine: Engine, clock: FakeClock, **overrides: Any) -> None:
        a_schedule(engine, **overrides)
        clock.advance(HOUR)
        engine.tick()

    def test_a_scheduled_session_can_be_skipped(self, engine: Engine, clock: FakeClock) -> None:
        self.running(engine, clock)

        response = engine.handle(Request(type="schedule.skip", payload={}))

        assert response.ok, response.error
        assert engine.state.session is None

    def test_and_does_not_come_straight_back(self, engine: Engine, clock: FakeClock) -> None:
        """Without remembering the skip, the next tick starts it again."""
        self.running(engine, clock)
        engine.handle(Request(type="schedule.skip", payload={}))

        clock.advance(60)
        engine.tick()

        assert engine.state.session is None

    def test_but_the_next_occurrence_still_runs(self, engine: Engine, clock: FakeClock) -> None:
        """A skip is for one morning, not for the schedule."""
        a_schedule(engine, days=frozenset({2, 3}))
        clock.advance(HOUR)
        engine.tick()
        engine.handle(Request(type="schedule.skip", payload={}))

        clock.advance(24 * HOUR)  # the same window, the next day
        engine.tick()

        assert engine.state.session is not None

    def test_every_skip_is_a_rupture(self, engine: Engine, clock: FakeClock) -> None:
        self.running(engine, clock)

        engine.handle(Request(type="schedule.skip", payload={}))

        assert any(rupture["kind"] == "skip" for rupture in engine.state.ruptures)

    def test_three_a_week_and_no_more(self, engine: Engine, clock: FakeClock) -> None:
        a_schedule(engine, days=frozenset(range(7)))
        for _ in range(3):
            clock.advance(HOUR)
            engine.tick()
            assert engine.handle(Request(type="schedule.skip", payload={})).ok
            clock.advance(23 * HOUR)

        clock.advance(HOUR)
        engine.tick()
        response = engine.handle(Request(type="schedule.skip", payload={}))

        assert not response.ok
        assert response.error["code"] == ErrorCode.SKIP_LIMIT_REACHED

    def test_they_come_back_on_monday(self, engine: Engine, clock: FakeClock) -> None:
        engine.state.skips_used = 3
        engine.state.skips_week_start = "2026-09-14"  # the week before
        self.running(engine, clock)

        assert engine.handle(Request(type="schedule.skip", payload={})).ok

    def test_a_strict_scheduled_session_cannot_be_skipped(
        self, engine: Engine, clock: FakeClock
    ) -> None:
        """SPEC 11: only its valve applies."""
        self.running(engine, clock, level=Level.STRICT, valve=Valve.WAIT)

        response = engine.handle(Request(type="schedule.skip", payload={}))

        assert not response.ok
        assert response.error["code"] == ErrorCode.SKIP_FORBIDDEN

    def test_a_manual_session_cannot_be_skipped(self, engine: Engine) -> None:
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

        response = engine.handle(Request(type="schedule.skip", payload={}))

        assert not response.ok
        assert "started by hand" in response.error["message"]

    def test_skipping_nothing_is_refused(self, engine: Engine) -> None:
        response = engine.handle(Request(type="schedule.skip", payload={}))

        assert not response.ok
        assert response.error["code"] == ErrorCode.NO_ACTIVE_SESSION


class TestEditingThem:
    def test_a_schedule_can_be_made_and_listed(self, engine: Engine) -> None:
        created = engine.handle(
            Request(
                type="schedule.create",
                payload={
                    "name": "Evenings",
                    "profile": "Work",
                    "days": ["monday", "tuesday"],
                    "start": "18:00",
                    "end": "20:00",
                    "level": "soft",
                    "valve": None,
                },
            )
        )

        assert created.ok, created.error
        listed = engine.handle(Request(type="schedule.list", payload={})).result
        assert listed["schedules"][0]["name"] == "Evenings"

    def test_one_that_is_not_running_can_be_edited(self, engine: Engine) -> None:
        schedule = a_schedule(engine)

        response = engine.handle(
            Request(
                type="schedule.edit",
                payload={
                    "id": schedule.id,
                    "name": "Later",
                    "start": "10:00",
                    "profile": None,
                    "days": None,
                    "end": None,
                    "level": None,
                    "valve": None,
                    "enabled": None,
                },
            )
        )

        assert response.ok, response.error
        assert engine.schedules[schedule.id].start_minute == 10 * 60

    def test_one_that_is_running_cannot(self, engine: Engine, clock: FakeClock) -> None:
        """SPEC 11: edited freely before it starts, which is not during."""
        schedule = a_schedule(engine)
        clock.advance(HOUR)
        engine.tick()

        response = engine.handle(
            Request(
                type="schedule.edit",
                payload={
                    "id": schedule.id,
                    "name": "Later",
                    "profile": None,
                    "days": None,
                    "start": None,
                    "end": None,
                    "level": None,
                    "valve": None,
                    "enabled": None,
                },
            )
        )

        assert not response.ok
        assert response.error["code"] == ErrorCode.RATCHET_VIOLATION

    def test_nor_deleted(self, engine: Engine, clock: FakeClock) -> None:
        schedule = a_schedule(engine)
        clock.advance(HOUR)
        engine.tick()

        response = engine.handle(Request(type="schedule.delete", payload={"id": schedule.id}))

        assert not response.ok

    def test_deleting_one_that_is_not_running_is_fine(self, engine: Engine) -> None:
        schedule = a_schedule(engine)

        assert engine.handle(Request(type="schedule.delete", payload={"id": schedule.id})).ok
        assert engine.schedules == {}

    def test_an_unknown_schedule_says_so(self, engine: Engine) -> None:
        response = engine.handle(Request(type="schedule.show", payload={"id": "nope"}))

        assert not response.ok
        assert response.error["code"] == ErrorCode.UNKNOWN_SCHEDULE

    def test_a_strict_schedule_without_a_valve_is_refused(self, engine: Engine) -> None:
        response = engine.handle(
            Request(
                type="schedule.create",
                payload={
                    "name": "Locked",
                    "profile": "Study",
                    "days": ["friday"],
                    "start": "09:00",
                    "end": "12:00",
                    "level": "strict",
                    "valve": None,
                },
            )
        )

        assert not response.ok
        assert "valve" in response.error["message"]


class TestSurvivingARestart:
    def test_schedules_are_read_back(
        self, engine: Engine, tmp_path: Path, clock: FakeClock
    ) -> None:
        a_schedule(engine)

        revived = Engine(Paths.resolve(tmp_path), Settings(owner_uid=os.getuid()), clock=clock)
        revived.load()

        assert len(revived.schedules) == 1

    def test_a_scheduled_session_keeps_its_rules_across_a_restart(
        self, engine: Engine, tmp_path: Path, clock: FakeClock
    ) -> None:
        a_schedule(engine)
        clock.advance(HOUR)
        engine.tick()

        revived = Engine(Paths.resolve(tmp_path), Settings(owner_uid=os.getuid()), clock=clock)
        revived.load()
        revived.profiles["Study"] = Profile(name="Study", domains=frozenset({"youtube.com"}))

        policy = revived.handle(Request(type="policy.get", payload={})).result
        assert policy["domains"] == ["youtube.com"]

    def test_a_broken_schedule_does_not_stop_the_engine(
        self, engine: Engine, tmp_path: Path, clock: FakeClock
    ) -> None:
        """One unreadable entry must not cost the others."""
        a_schedule(engine)
        raw = engine._config_store.load().data  # noqa: SLF001
        raw["schedules"].append({"id": "broken", "name": "", "profile": "X"})
        engine._config_store.save(raw)  # noqa: SLF001

        revived = Engine(Paths.resolve(tmp_path), Settings(owner_uid=os.getuid()), clock=clock)
        revived.load()

        assert len(revived.schedules) == 1


class TestTheCommandLine:
    """`anchor schedule` and `anchor skip` (SPEC 11, 15)."""

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

    @pytest.fixture
    def served(self, engine: Engine, tmp_path: Path) -> Any:
        import threading

        from anchor.engine.service import EngineServer

        server = EngineServer(engine)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield Paths.resolve(tmp_path)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_a_schedule_can_be_made_from_the_terminal(self, served: Paths, engine: Engine) -> None:
        code, out, err = self.cli(
            served,
            "schedule",
            "create",
            "Evenings",
            "--profile",
            "Work",
            "--day",
            "monday",
            "--day",
            "wednesday",
            "--from",
            "18:00",
            "--to",
            "20:00",
        )

        assert code == 0, err
        assert len(engine.schedules) == 1

    def test_and_listed(self, served: Paths, engine: Engine) -> None:
        a_schedule(engine)

        code, out, err = self.cli(served, "schedule", "list")

        assert code == 0, err
        assert "Mornings" in out
        assert "Wed" in out
        assert "Skips left this week: 3 of 3" in out

    def test_a_running_one_is_marked(self, served: Paths, engine: Engine, clock: FakeClock) -> None:
        a_schedule(engine)
        clock.advance(HOUR)
        engine.tick()

        _code, out, _err = self.cli(served, "schedule", "list")

        assert "running now" in out

    def test_and_deleted(self, served: Paths, engine: Engine) -> None:
        schedule = a_schedule(engine)

        code, _out, err = self.cli(served, "schedule", "delete", schedule.id)

        assert code == 0, err
        assert engine.schedules == {}

    def test_skipping_from_the_terminal(
        self, served: Paths, engine: Engine, clock: FakeClock
    ) -> None:
        a_schedule(engine)
        clock.advance(HOUR)
        engine.tick()

        code, _out, err = self.cli(served, "skip")

        assert code == 0, err
        assert engine.state.session is None

    def test_a_refused_skip_carries_the_exit_code_for_one(
        self, served: Paths, engine: Engine, clock: FakeClock
    ) -> None:
        a_schedule(engine, level=Level.STRICT, valve=Valve.WAIT)
        clock.advance(HOUR)
        engine.tick()

        code, _out, err = self.cli(served, "skip")

        assert code == 4
        assert "cannot be skipped" in err

    def test_editing_a_running_schedule_is_refused_clearly(
        self, served: Paths, engine: Engine, clock: FakeClock
    ) -> None:
        schedule = a_schedule(engine)
        clock.advance(HOUR)
        engine.tick()

        code, _out, err = self.cli(served, "schedule", "edit", schedule.id, "--name", "X")

        assert code == 4
        assert "running now" in err
