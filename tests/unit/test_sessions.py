"""Starting, extending and leaving sessions (SPEC 7.1 to 7.5)."""

from __future__ import annotations

import pytest

from anchor.engine.sessions import (
    ExitKind,
    Session,
    SessionPolicy,
    start_session,
)
from anchor.engine.timekeeping import FakeClock
from anchor.protocol.errors import AnchorError, ErrorCode
from anchor.protocol.types import Level, SessionOrigin, SessionPhase, Valve

HOUR = 3600.0
START_WALL = 1_760_000_000.0


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(wall_time=START_WALL, boottime=1_000.0, boot="boot-a")


@pytest.fixture
def policy() -> SessionPolicy:
    return SessionPolicy()


def begin(
    clock: FakeClock,
    policy: SessionPolicy,
    *,
    level: Level = Level.SOFT,
    duration: float = 2 * HOUR,
    valve: Valve | None = None,
    origin: SessionOrigin = SessionOrigin.MANUAL,
) -> Session:
    return start_session(
        profile="Study",
        level=level,
        duration_seconds=duration,
        valve=valve,
        origin=origin,
        clock=clock,
        policy=policy,
    )


class TestStarting:
    def test_a_manual_session_may_not_exceed_eight_hours(
        self, clock: FakeClock, policy: SessionPolicy
    ) -> None:
        with pytest.raises(AnchorError) as caught:
            begin(clock, policy, duration=9 * HOUR)
        assert caught.value.code is ErrorCode.DURATION_TOO_LONG

    def test_exactly_eight_hours_is_allowed(self, clock: FakeClock, policy: SessionPolicy) -> None:
        assert begin(clock, policy, duration=8 * HOUR).anchor.duration_seconds == 8 * HOUR

    def test_the_cap_does_not_apply_to_scheduled_sessions(
        self, clock: FakeClock, policy: SessionPolicy
    ) -> None:
        """SPEC 11: the 8-hour cap does not apply to schedules."""
        session = begin(clock, policy, duration=10 * HOUR, origin=SessionOrigin.SCHEDULE)
        assert session.anchor.duration_seconds == 10 * HOUR

    def test_a_strict_session_needs_a_valve(self, clock: FakeClock, policy: SessionPolicy) -> None:
        with pytest.raises(AnchorError) as caught:
            begin(clock, policy, level=Level.STRICT, valve=None)
        assert caught.value.code is ErrorCode.BAD_REQUEST

    def test_a_valve_is_meaningless_below_strict(
        self, clock: FakeClock, policy: SessionPolicy
    ) -> None:
        with pytest.raises(AnchorError) as caught:
            begin(clock, policy, level=Level.FIRM, valve=Valve.WAIT)
        assert caught.value.code is ErrorCode.BAD_REQUEST

    def test_a_new_session_starts_working(self, clock: FakeClock, policy: SessionPolicy) -> None:
        session = begin(clock, policy)
        assert session.phase is SessionPhase.WORKING
        assert session.exit_request is None
        assert session.blocked_attempts == 0


class TestExtending:
    def test_any_session_can_be_extended(self, clock: FakeClock, policy: SessionPolicy) -> None:
        session = begin(clock, policy, duration=HOUR)
        assert session.extended_by(30 * 60).anchor.duration_seconds == 1.5 * HOUR

    def test_extending_past_eight_hours_is_allowed(
        self, clock: FakeClock, policy: SessionPolicy
    ) -> None:
        """The cap applies only at start (SPEC 7.3)."""
        session = begin(clock, policy, duration=8 * HOUR)
        assert session.extended_by(2 * HOUR).anchor.duration_seconds == 10 * HOUR

    def test_shortening_is_not_an_extension(self, clock: FakeClock, policy: SessionPolicy) -> None:
        session = begin(clock, policy)
        with pytest.raises(AnchorError) as caught:
            session.extended_by(-600)
        assert caught.value.code is ErrorCode.INVALID_DURATION


class TestLeavingSoft:
    def test_cancelling_needs_a_five_minute_wait(
        self, clock: FakeClock, policy: SessionPolicy
    ) -> None:
        session = begin(clock, policy, level=Level.SOFT)
        pending = session.request_exit(ExitKind.CANCEL, clock=clock, policy=policy)

        assert pending.exit_request is not None
        assert pending.exit_request.wait_seconds == 300
        assert pending.exit_request.phrase is None

    def test_the_wait_must_actually_elapse(self, clock: FakeClock, policy: SessionPolicy) -> None:
        session = begin(clock, policy, level=Level.SOFT)
        pending = session.request_exit(ExitKind.CANCEL, clock=clock, policy=policy)

        clock.advance(299)
        with pytest.raises(AnchorError) as caught:
            pending.complete_exit(clock=clock)
        assert caught.value.code is ErrorCode.WAIT_NOT_ELAPSED

        clock.advance(2)
        assert pending.complete_exit(clock=clock).ended

    def test_a_pending_cancellation_can_be_withdrawn(
        self, clock: FakeClock, policy: SessionPolicy
    ) -> None:
        session = begin(clock, policy, level=Level.SOFT)
        pending = session.request_exit(ExitKind.CANCEL, clock=clock, policy=policy)
        assert pending.withdraw_exit().exit_request is None

    def test_leaving_a_soft_session_is_not_a_rupture(
        self, clock: FakeClock, policy: SessionPolicy
    ) -> None:
        """Only valve use, skips and tampering are ruptures (SPEC 4)."""
        session = begin(clock, policy, level=Level.SOFT)
        pending = session.request_exit(ExitKind.CANCEL, clock=clock, policy=policy)
        clock.advance(301)
        assert pending.complete_exit(clock=clock).rupture is None


class TestLeavingFirm:
    def test_cancelling_needs_a_long_wait_and_a_typed_phrase(
        self, clock: FakeClock, policy: SessionPolicy
    ) -> None:
        session = begin(clock, policy, level=Level.FIRM)
        pending = session.request_exit(ExitKind.CANCEL, clock=clock, policy=policy)

        assert pending.exit_request is not None
        assert pending.exit_request.wait_seconds == 15 * 60
        assert pending.exit_request.phrase is not None
        assert len(pending.exit_request.phrase) >= 150

    def test_the_phrase_must_match(self, clock: FakeClock, policy: SessionPolicy) -> None:
        session = begin(clock, policy, level=Level.FIRM)
        pending = session.request_exit(ExitKind.CANCEL, clock=clock, policy=policy)
        clock.advance(16 * 60)

        with pytest.raises(AnchorError) as caught:
            pending.complete_exit(clock=clock, typed="not the phrase")
        assert caught.value.code is ErrorCode.PHRASE_MISMATCH

    def test_a_missing_phrase_is_refused(self, clock: FakeClock, policy: SessionPolicy) -> None:
        session = begin(clock, policy, level=Level.FIRM)
        pending = session.request_exit(ExitKind.CANCEL, clock=clock, policy=policy)
        clock.advance(16 * 60)

        with pytest.raises(AnchorError) as caught:
            pending.complete_exit(clock=clock)
        assert caught.value.code is ErrorCode.PHRASE_MISMATCH

    def test_the_right_phrase_after_the_wait_ends_the_session(
        self, clock: FakeClock, policy: SessionPolicy
    ) -> None:
        session = begin(clock, policy, level=Level.FIRM)
        pending = session.request_exit(ExitKind.CANCEL, clock=clock, policy=policy)
        assert pending.exit_request is not None
        clock.advance(16 * 60)

        outcome = pending.complete_exit(clock=clock, typed=pending.exit_request.phrase)
        assert outcome.ended
        assert outcome.rupture is None

    def test_surrounding_whitespace_in_the_typed_phrase_is_forgiven(
        self, clock: FakeClock, policy: SessionPolicy
    ) -> None:
        session = begin(clock, policy, level=Level.FIRM)
        pending = session.request_exit(ExitKind.CANCEL, clock=clock, policy=policy)
        assert pending.exit_request is not None
        clock.advance(16 * 60)

        typed = f"  {pending.exit_request.phrase}\n"
        assert pending.complete_exit(clock=clock, typed=typed).ended


class TestLeavingStrict:
    def test_a_strict_session_cannot_be_cancelled(
        self, clock: FakeClock, policy: SessionPolicy
    ) -> None:
        session = begin(clock, policy, level=Level.STRICT, valve=Valve.WAIT)
        with pytest.raises(AnchorError) as caught:
            session.request_exit(ExitKind.CANCEL, clock=clock, policy=policy)
        assert caught.value.code is ErrorCode.CANCEL_FORBIDDEN

    def test_the_wait_valve_takes_thirty_minutes(
        self, clock: FakeClock, policy: SessionPolicy
    ) -> None:
        session = begin(clock, policy, level=Level.STRICT, valve=Valve.WAIT)
        pending = session.request_exit(ExitKind.VALVE, clock=clock, policy=policy)

        assert pending.exit_request is not None
        assert pending.exit_request.wait_seconds == 30 * 60
        assert pending.exit_request.phrase is None

    def test_the_phrase_valve_has_no_wait(self, clock: FakeClock, policy: SessionPolicy) -> None:
        session = begin(clock, policy, level=Level.STRICT, valve=Valve.PHRASE)
        pending = session.request_exit(ExitKind.VALVE, clock=clock, policy=policy)

        assert pending.exit_request is not None
        assert pending.exit_request.wait_seconds == 0
        assert pending.exit_request.phrase is not None

    def test_the_both_valve_asks_for_wait_and_phrase(
        self, clock: FakeClock, policy: SessionPolicy
    ) -> None:
        session = begin(clock, policy, level=Level.STRICT, valve=Valve.BOTH)
        pending = session.request_exit(ExitKind.VALVE, clock=clock, policy=policy)

        assert pending.exit_request is not None
        assert pending.exit_request.wait_seconds == 30 * 60
        assert pending.exit_request.phrase is not None

    def test_a_valve_request_can_be_withdrawn(
        self, clock: FakeClock, policy: SessionPolicy
    ) -> None:
        """SPEC 7.5: the request can be withdrawn."""
        session = begin(clock, policy, level=Level.STRICT, valve=Valve.WAIT)
        pending = session.request_exit(ExitKind.VALVE, clock=clock, policy=policy)
        assert pending.withdraw_exit().exit_request is None

    def test_using_the_valve_is_always_a_rupture(
        self, clock: FakeClock, policy: SessionPolicy
    ) -> None:
        """SPEC 7.5: every valve use is a rupture."""
        session = begin(clock, policy, level=Level.STRICT, valve=Valve.WAIT)
        pending = session.request_exit(ExitKind.VALVE, clock=clock, policy=policy)
        clock.advance(31 * 60)

        outcome = pending.complete_exit(clock=clock)
        assert outcome.ended
        assert outcome.rupture is not None
        assert outcome.rupture.kind.value == "valve"

    def test_completing_without_a_request_is_refused(
        self, clock: FakeClock, policy: SessionPolicy
    ) -> None:
        session = begin(clock, policy, level=Level.STRICT, valve=Valve.WAIT)
        with pytest.raises(AnchorError) as caught:
            session.complete_exit(clock=clock)
        assert caught.value.code is ErrorCode.VALVE_NOT_REQUESTED

    def test_a_fresh_phrase_is_generated_for_every_request(
        self, clock: FakeClock, policy: SessionPolicy
    ) -> None:
        """SPEC 7.5: a long phrase generated fresh each time."""
        session = begin(clock, policy, level=Level.STRICT, valve=Valve.PHRASE)

        first = session.request_exit(ExitKind.VALVE, clock=clock, policy=policy)
        second = first.withdraw_exit().request_exit(ExitKind.VALVE, clock=clock, policy=policy)

        assert first.exit_request is not None
        assert second.exit_request is not None
        assert first.exit_request.phrase != second.exit_request.phrase


class TestPolicyIsConfigurable:
    def test_the_firm_wait_and_phrase_length_can_be_changed(self, clock: FakeClock) -> None:
        """SPEC 7.2: both configurable, never during a session."""
        policy = SessionPolicy(firm_wait_seconds=60, phrase_length=20)
        session = begin(clock, policy, level=Level.FIRM)
        pending = session.request_exit(ExitKind.CANCEL, clock=clock, policy=policy)

        assert pending.exit_request is not None
        assert pending.exit_request.wait_seconds == 60
        assert len(pending.exit_request.phrase or "") >= 20
