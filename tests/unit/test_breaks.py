"""The break timer: cycles, long breaks, skipping and absences (SPEC 10).

Written before the module they test. Breaks are the part of Anchor most likely
to be wrong in a way nobody notices for an hour, so the behaviour is pinned
here first and the implementation made to match.
"""

from __future__ import annotations

import pytest

from anchor.engine.breaks import (
    POSTPONE_SECONDS,
    BreakState,
    advance,
    postpone,
    skip,
)
from anchor.engine.profiles import BreakSettings
from anchor.engine.timekeeping import FakeClock
from anchor.protocol.errors import AnchorError, ErrorCode
from anchor.protocol.types import BreakHardness, SessionPhase

MINUTE = 60


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(wall_time=1_760_000_000.0, boottime=1_000.0, boot="boot-a")


def settings(**overrides: object) -> BreakSettings:
    base: dict[str, object] = {"work_minutes": 50, "break_minutes": 10, "warning_seconds": 60}
    base.update(overrides)
    return BreakSettings(**base)  # type: ignore[arg-type]


def run_for(
    state: BreakState, pattern: BreakSettings, clock: FakeClock, seconds: float, step: float = 30.0
) -> tuple[BreakState, list[tuple[str, dict[str, object]]]]:
    """Tick the way the engine does, and collect everything published."""
    events: list[tuple[str, dict[str, object]]] = []
    passed = 0.0
    while passed < seconds:
        clock.advance(step)
        passed += step
        outcome = advance(state, pattern, clock)
        state = outcome.state
        events.extend(outcome.events)
    return state, events


def names(events: list[tuple[str, dict[str, object]]]) -> list[str]:
    return [name for name, _ in events]


class TestASessionStarts:
    def test_it_starts_working_rather_than_resting(self, clock: FakeClock) -> None:
        state = BreakState.start(settings(), clock)

        assert state.phase is SessionPhase.WORKING
        assert state.cycles_done == 0

    def test_the_first_break_is_a_whole_work_period_away(self, clock: FakeClock) -> None:
        state = BreakState.start(settings(work_minutes=50), clock)

        assert state.remaining(clock) == pytest.approx(50 * MINUTE)


class TestTheWarning:
    def test_it_comes_before_the_break(self, clock: FakeClock) -> None:
        """ADR 2: a break must never arrive unannounced."""
        pattern = settings(work_minutes=25, warning_seconds=60)
        state = BreakState.start(pattern, clock)

        state, events = run_for(state, pattern, clock, 24 * MINUTE)

        assert names(events) == ["break.warning"]
        assert state.phase is SessionPhase.WORKING

    def test_it_says_how_long_is_left(self, clock: FakeClock) -> None:
        pattern = settings(work_minutes=25, warning_seconds=120)
        state = BreakState.start(pattern, clock)

        _state, events = run_for(state, pattern, clock, 23 * MINUTE + 30)

        assert events[0][1]["seconds"] == pytest.approx(120, abs=31)

    def test_it_comes_once_and_not_once_a_second(self, clock: FakeClock) -> None:
        pattern = settings(work_minutes=25, warning_seconds=300)
        state = BreakState.start(pattern, clock)

        _state, events = run_for(state, pattern, clock, 24 * MINUTE, step=10)

        assert names(events).count("break.warning") == 1

    def test_turning_it_off_means_no_warning(self, clock: FakeClock) -> None:
        pattern = settings(work_minutes=25, warning_seconds=0)
        state = BreakState.start(pattern, clock)

        _state, events = run_for(state, pattern, clock, 24 * MINUTE)

        assert names(events) == []


class TestTheBreakItself:
    def test_it_starts_when_the_work_period_ends(self, clock: FakeClock) -> None:
        pattern = settings(work_minutes=25, break_minutes=5)
        state = BreakState.start(pattern, clock)

        state, events = run_for(state, pattern, clock, 25 * MINUTE)

        assert state.phase is SessionPhase.BREAK
        assert "break.started" in names(events)

    def test_it_lasts_the_profiles_break(self, clock: FakeClock) -> None:
        pattern = settings(work_minutes=25, break_minutes=5)
        state = BreakState.start(pattern, clock)
        state, _ = run_for(state, pattern, clock, 25 * MINUTE)

        assert state.remaining(clock) == pytest.approx(5 * MINUTE, abs=31)

    def test_it_ends_on_its_own(self, clock: FakeClock) -> None:
        pattern = settings(work_minutes=25, break_minutes=5)
        state = BreakState.start(pattern, clock)

        state, events = run_for(state, pattern, clock, 30 * MINUTE + MINUTE)

        assert state.phase is SessionPhase.WORKING
        assert "break.ended" in names(events)
        assert state.taken == 1

    def test_the_next_work_period_is_a_full_one(self, clock: FakeClock) -> None:
        pattern = settings(work_minutes=25, break_minutes=5)
        state = BreakState.start(pattern, clock)
        state, _ = run_for(state, pattern, clock, 30 * MINUTE + MINUTE)

        assert state.remaining(clock) == pytest.approx(25 * MINUTE, abs=61)

    def test_the_cycle_repeats(self, clock: FakeClock) -> None:
        pattern = settings(work_minutes=25, break_minutes=5)
        state = BreakState.start(pattern, clock)

        state, events = run_for(state, pattern, clock, 3 * 30 * MINUTE)

        assert names(events).count("break.started") == 3
        assert state.taken == 3


class TestLongBreaks:
    def test_every_nth_break_is_longer(self, clock: FakeClock) -> None:
        """SPEC 10: an optional long break every N cycles."""
        pattern = settings(
            work_minutes=25,
            break_minutes=5,
            long_break_every=2,
            long_break_minutes=15,
        )
        state = BreakState.start(pattern, clock)

        # Through the first break, which is ordinary.
        state, _ = run_for(state, pattern, clock, 26 * MINUTE)
        assert state.remaining(clock) == pytest.approx(5 * MINUTE, abs=61)

        # And on to the second, which is not.
        state, events = run_for(state, pattern, clock, 30 * MINUTE)
        assert state.phase is SessionPhase.BREAK
        assert state.long is True
        assert state.remaining(clock) == pytest.approx(15 * MINUTE, abs=61)

    def test_the_event_says_which_kind_it_is(self, clock: FakeClock) -> None:
        pattern = settings(
            work_minutes=25, break_minutes=5, long_break_every=1, long_break_minutes=15
        )
        state = BreakState.start(pattern, clock)

        _state, events = run_for(state, pattern, clock, 26 * MINUTE)

        started = [payload for name, payload in events if name == "break.started"]
        assert started[0]["long"] is True
        assert started[0]["seconds"] == pytest.approx(15 * MINUTE)

    def test_without_the_setting_every_break_is_the_same(self, clock: FakeClock) -> None:
        pattern = settings(work_minutes=25, break_minutes=5)
        state = BreakState.start(pattern, clock)

        state, _ = run_for(state, pattern, clock, 4 * 30 * MINUTE + MINUTE)

        assert state.long is False


class TestSkipping:
    def pattern(self, hardness: BreakHardness) -> BreakSettings:
        return settings(work_minutes=25, break_minutes=5, hardness=hardness)

    def on_a_break(self, clock: FakeClock, pattern: BreakSettings) -> BreakState:
        state = BreakState.start(pattern, clock)
        state, _ = run_for(state, pattern, clock, 25 * MINUTE)
        assert state.phase is SessionPhase.BREAK
        return state

    def test_a_flexible_break_can_be_skipped(self, clock: FakeClock) -> None:
        pattern = self.pattern(BreakHardness.FLEXIBLE)
        state = self.on_a_break(clock, pattern)

        state = skip(state, pattern, clock)

        assert state.phase is SessionPhase.WORKING
        assert state.skipped == 1
        assert state.taken == 0

    def test_a_moderate_break_cannot(self, clock: FakeClock) -> None:
        pattern = self.pattern(BreakHardness.MODERATE)
        state = self.on_a_break(clock, pattern)

        with pytest.raises(AnchorError) as raised:
            skip(state, pattern, clock)

        assert raised.value.code is ErrorCode.SKIP_FORBIDDEN

    def test_a_mandatory_break_cannot(self, clock: FakeClock) -> None:
        pattern = self.pattern(BreakHardness.MANDATORY)
        state = self.on_a_break(clock, pattern)

        with pytest.raises(AnchorError):
            skip(state, pattern, clock)

    def test_skipping_when_there_is_no_break_is_refused(self, clock: FakeClock) -> None:
        pattern = self.pattern(BreakHardness.FLEXIBLE)
        state = BreakState.start(pattern, clock)

        with pytest.raises(AnchorError):
            skip(state, pattern, clock)

    def test_a_skipped_break_starts_a_fresh_work_period(self, clock: FakeClock) -> None:
        pattern = self.pattern(BreakHardness.FLEXIBLE)
        state = skip(self.on_a_break(clock, pattern), pattern, clock)

        assert state.remaining(clock) == pytest.approx(25 * MINUTE)


class TestPostponing:
    def pattern(self, hardness: BreakHardness) -> BreakSettings:
        return settings(work_minutes=25, break_minutes=5, hardness=hardness)

    def on_a_break(self, clock: FakeClock, pattern: BreakSettings) -> BreakState:
        state = BreakState.start(pattern, clock)
        state, _ = run_for(state, pattern, clock, 25 * MINUTE)
        return state

    def test_it_pushes_the_break_back(self, clock: FakeClock) -> None:
        pattern = self.pattern(BreakHardness.MODERATE)
        state = postpone(self.on_a_break(clock, pattern), pattern, clock)

        assert state.phase is SessionPhase.WORKING
        assert state.remaining(clock) == pytest.approx(POSTPONE_SECONDS)

    def test_the_break_is_still_owed(self, clock: FakeClock) -> None:
        """Postponing is not skipping: it comes back."""
        pattern = self.pattern(BreakHardness.MODERATE)
        state = postpone(self.on_a_break(clock, pattern), pattern, clock)

        state, events = run_for(state, pattern, clock, POSTPONE_SECONDS + MINUTE)

        assert state.phase is SessionPhase.BREAK
        assert "break.started" in names(events)

    def test_moderate_allows_it_once(self, clock: FakeClock) -> None:
        pattern = self.pattern(BreakHardness.MODERATE)
        state = postpone(self.on_a_break(clock, pattern), pattern, clock)
        state, _ = run_for(state, pattern, clock, POSTPONE_SECONDS + MINUTE)

        with pytest.raises(AnchorError) as raised:
            postpone(state, pattern, clock)

        assert raised.value.code is ErrorCode.SKIP_FORBIDDEN

    def test_flexible_allows_it_again_and_again(self, clock: FakeClock) -> None:
        pattern = self.pattern(BreakHardness.FLEXIBLE)
        state = self.on_a_break(clock, pattern)

        for _ in range(4):
            state = postpone(state, pattern, clock)
            state, _ = run_for(state, pattern, clock, POSTPONE_SECONDS + MINUTE)

        assert state.postponed_total == 4

    def test_mandatory_refuses(self, clock: FakeClock) -> None:
        pattern = self.pattern(BreakHardness.MANDATORY)
        state = self.on_a_break(clock, pattern)

        with pytest.raises(AnchorError):
            postpone(state, pattern, clock)

    def test_taking_the_break_forgives_the_postponements(self, clock: FakeClock) -> None:
        """The limit is per break, not per session."""
        pattern = self.pattern(BreakHardness.MODERATE)
        state = postpone(self.on_a_break(clock, pattern), pattern, clock)
        state, _ = run_for(state, pattern, clock, POSTPONE_SECONDS + 6 * MINUTE)
        assert state.taken == 1

        state, _ = run_for(state, pattern, clock, 26 * MINUTE)

        assert postpone(state, pattern, clock).phase is SessionPhase.WORKING

    def test_postponing_when_there_is_no_break_is_refused(self, clock: FakeClock) -> None:
        pattern = self.pattern(BreakHardness.FLEXIBLE)

        with pytest.raises(AnchorError):
            postpone(BreakState.start(pattern, clock), pattern, clock)


class TestAnAbsence:
    """SPEC 10: the work timer keeps running during idle and suspend."""

    def test_a_short_suspend_only_moves_the_clock_on(self, clock: FakeClock) -> None:
        pattern = settings(work_minutes=50, break_minutes=10)
        state = BreakState.start(pattern, clock)

        clock.suspend(10 * MINUTE)
        outcome = advance(state, pattern, clock)

        assert outcome.state.phase is SessionPhase.WORKING
        assert outcome.state.remaining(clock) == pytest.approx(40 * MINUTE)

    def test_an_absence_covering_a_break_counts_it_as_taken(self, clock: FakeClock) -> None:
        """SPEC 10 says so outright, and it is the humane reading."""
        pattern = settings(work_minutes=50, break_minutes=10)
        state = BreakState.start(pattern, clock)

        clock.suspend(70 * MINUTE)
        outcome = advance(state, pattern, clock)

        assert outcome.state.phase is SessionPhase.WORKING
        assert outcome.state.taken == 1
        assert outcome.state.cycles_done == 1

    def test_and_starts_a_fresh_work_period(self, clock: FakeClock) -> None:
        pattern = settings(work_minutes=50, break_minutes=10)
        state = BreakState.start(pattern, clock)

        clock.suspend(70 * MINUTE)
        outcome = advance(state, pattern, clock)

        assert outcome.state.remaining(clock) == pytest.approx(50 * MINUTE)

    def test_a_long_absence_is_not_replayed_cycle_by_cycle(self, clock: FakeClock) -> None:
        """Eight hours asleep is one missed break, not nine."""
        pattern = settings(work_minutes=50, break_minutes=10)
        state = BreakState.start(pattern, clock)

        clock.suspend(8 * 3600)
        outcome = advance(state, pattern, clock)

        assert outcome.state.taken == 1
        assert outcome.events == ()

    def test_an_absence_during_a_break_ends_it_quietly(self, clock: FakeClock) -> None:
        pattern = settings(work_minutes=50, break_minutes=10)
        state = BreakState.start(pattern, clock)
        state, _ = run_for(state, pattern, clock, 50 * MINUTE)
        assert state.phase is SessionPhase.BREAK

        clock.suspend(2 * 3600)
        outcome = advance(state, pattern, clock)

        assert outcome.state.phase is SessionPhase.WORKING
        assert outcome.state.taken == 1

    def test_a_reboot_does_not_lose_the_cycle(self, clock: FakeClock) -> None:
        """SPEC 6.2: the boot clock restarts, the session does not."""
        pattern = settings(work_minutes=50, break_minutes=10)
        state = BreakState.start(pattern, clock)
        clock.advance(20 * MINUTE)
        state = advance(state, pattern, clock).state

        clock.reboot(new_boot="boot-b", wall_elapsed=5 * MINUTE, boottime_at_start=12.0)
        outcome = advance(state, pattern, clock)

        assert outcome.state.phase is SessionPhase.WORKING
        assert outcome.state.remaining(clock) == pytest.approx(25 * MINUTE, abs=61)


class TestWhatTheInterfaceNeeds:
    def test_the_time_of_the_next_break_is_available(self, clock: FakeClock) -> None:
        """SPEC 14.1: the menu shows when the next break falls."""
        pattern = settings(work_minutes=25, break_minutes=5)
        state = BreakState.start(pattern, clock)

        assert state.ends_at == pytest.approx(clock.wall() + 25 * MINUTE)

    def test_the_counters_are_the_ones_statistics_wants(self, clock: FakeClock) -> None:
        """SPEC 13: breaks taken, postponed and skipped."""
        pattern = settings(work_minutes=25, break_minutes=5, hardness=BreakHardness.FLEXIBLE)
        state = BreakState.start(pattern, clock)
        state, _ = run_for(state, pattern, clock, 25 * MINUTE)
        state = postpone(state, pattern, clock)
        state, _ = run_for(state, pattern, clock, POSTPONE_SECONDS + MINUTE)
        state = skip(state, pattern, clock)
        state, _ = run_for(state, pattern, clock, 31 * MINUTE)

        assert (state.taken, state.postponed_total, state.skipped) == (1, 1, 1)


class TestRoundTrip:
    def test_the_state_survives_being_written_and_read(self, clock: FakeClock) -> None:
        """It lives in state.json, and a restart must not lose the cycle."""
        pattern = settings(work_minutes=25, break_minutes=5)
        state = BreakState.start(pattern, clock)
        state, _ = run_for(state, pattern, clock, 26 * MINUTE)

        restored = BreakState.from_dict(state.to_dict())

        assert restored == state
