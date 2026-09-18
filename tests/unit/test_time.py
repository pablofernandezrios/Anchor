"""Time reconciliation across suspends, boots and clock changes (SPEC 6.2)."""

from __future__ import annotations

import pytest

from anchor.engine.timekeeping import (
    CLOCK_TOLERANCE_SECONDS,
    FakeClock,
    TimeAnchor,
    reconcile,
)

HOUR = 3600.0
START_WALL = 1_760_000_000.0  # an arbitrary but fixed instant


def anchor_at(clock: FakeClock, duration: float) -> TimeAnchor:
    return TimeAnchor.start(clock, duration_seconds=duration)


def test_ends_at_is_an_absolute_utc_timestamp() -> None:
    clock = FakeClock(wall_time=START_WALL, boottime=100.0, boot="boot-a")
    anchor = anchor_at(clock, 2 * HOUR)
    assert anchor.ends_at == START_WALL + 2 * HOUR


def test_remaining_time_shrinks_as_time_passes() -> None:
    clock = FakeClock(wall_time=START_WALL, boottime=100.0, boot="boot-a")
    anchor = anchor_at(clock, 2 * HOUR)

    clock.advance(30 * 60)
    outcome = reconcile(anchor, clock)

    assert not outcome.expired
    assert outcome.remaining_seconds == pytest.approx(1.5 * HOUR)
    assert not outcome.tampered


def test_time_runs_during_suspend() -> None:
    """A suspended laptop keeps burning session time (SPEC 6.2)."""
    clock = FakeClock(wall_time=START_WALL, boottime=100.0, boot="boot-a")
    anchor = anchor_at(clock, 2 * HOUR)

    # CLOCK_BOOTTIME keeps counting while the machine sleeps, and so does the
    # wall clock, so both move together across a suspend.
    clock.suspend(3 * HOUR)
    outcome = reconcile(anchor, clock)

    assert outcome.expired
    assert outcome.remaining_seconds == 0.0
    assert not outcome.tampered


def test_session_survives_a_short_suspend() -> None:
    clock = FakeClock(wall_time=START_WALL, boottime=100.0, boot="boot-a")
    anchor = anchor_at(clock, 2 * HOUR)

    clock.suspend(HOUR)
    outcome = reconcile(anchor, clock)

    assert not outcome.expired
    assert outcome.remaining_seconds == pytest.approx(HOUR)


def test_winding_the_clock_back_is_tampering_and_does_not_extend_the_session() -> None:
    """The boot-time reading wins over a wall clock that disagrees (SPEC 6.2)."""
    clock = FakeClock(wall_time=START_WALL, boottime=100.0, boot="boot-a")
    anchor = anchor_at(clock, 2 * HOUR)

    clock.advance(90 * 60)
    clock.warp_wall_clock(-6 * HOUR)  # "it is still the morning"
    outcome = reconcile(anchor, clock)

    assert outcome.tampered
    assert outcome.remaining_seconds == pytest.approx(30 * 60)
    assert not outcome.expired


def test_winding_the_clock_forward_does_not_end_the_session_early() -> None:
    clock = FakeClock(wall_time=START_WALL, boottime=100.0, boot="boot-a")
    anchor = anchor_at(clock, 2 * HOUR)

    clock.advance(10 * 60)
    clock.warp_wall_clock(5 * HOUR)  # "the session is surely over by now"
    outcome = reconcile(anchor, clock)

    assert outcome.tampered
    assert not outcome.expired
    assert outcome.remaining_seconds == pytest.approx(110 * 60)


def test_reconciling_after_tampering_reanchors_the_absolute_end() -> None:
    """The stored ``ends_at`` follows the boot clock, so it stays usable."""
    clock = FakeClock(wall_time=START_WALL, boottime=100.0, boot="boot-a")
    anchor = anchor_at(clock, 2 * HOUR)

    clock.advance(30 * 60)
    clock.warp_wall_clock(-6 * HOUR)
    outcome = reconcile(anchor, clock)

    assert outcome.anchor.ends_at == pytest.approx(clock.wall() + 1.5 * HOUR)
    # The re-anchored value is stable: reconciling again reports no new tamper.
    again = reconcile(outcome.anchor, clock)
    assert not again.tampered
    assert again.remaining_seconds == pytest.approx(1.5 * HOUR)


def test_small_clock_corrections_are_not_tampering() -> None:
    """Normal time synchronisation must not raise a rupture."""
    clock = FakeClock(wall_time=START_WALL, boottime=100.0, boot="boot-a")
    anchor = anchor_at(clock, 2 * HOUR)

    clock.advance(600)
    clock.warp_wall_clock(CLOCK_TOLERANCE_SECONDS / 2)
    outcome = reconcile(anchor, clock)

    assert not outcome.tampered


def test_after_a_reboot_the_wall_clock_decides() -> None:
    """CLOCK_BOOTTIME restarts on boot, so the absolute end is all we have."""
    clock = FakeClock(wall_time=START_WALL, boottime=5_000.0, boot="boot-a")
    anchor = anchor_at(clock, 2 * HOUR)

    clock.reboot(new_boot="boot-b", wall_elapsed=HOUR, boottime_at_start=30.0)
    outcome = reconcile(anchor, clock)

    assert outcome.rebooted
    assert not outcome.expired
    assert outcome.remaining_seconds == pytest.approx(HOUR)


def test_a_session_that_expired_while_the_machine_was_off_is_over() -> None:
    clock = FakeClock(wall_time=START_WALL, boottime=5_000.0, boot="boot-a")
    anchor = anchor_at(clock, 2 * HOUR)

    clock.reboot(new_boot="boot-b", wall_elapsed=3 * HOUR, boottime_at_start=30.0)
    outcome = reconcile(anchor, clock)

    assert outcome.expired
    assert outcome.remaining_seconds == 0.0


def test_reboot_reanchors_to_the_new_boot_clock() -> None:
    clock = FakeClock(wall_time=START_WALL, boottime=5_000.0, boot="boot-a")
    anchor = anchor_at(clock, 2 * HOUR)

    clock.reboot(new_boot="boot-b", wall_elapsed=HOUR, boottime_at_start=30.0)
    outcome = reconcile(anchor, clock)

    assert outcome.anchor.boot_id == "boot-b"
    assert outcome.anchor.started_at_boottime == pytest.approx(30.0 - HOUR)

    clock.advance(30 * 60)
    again = reconcile(outcome.anchor, clock)
    assert not again.tampered
    assert again.remaining_seconds == pytest.approx(30 * 60)


def test_extending_moves_both_the_absolute_and_the_boot_deadline() -> None:
    clock = FakeClock(wall_time=START_WALL, boottime=100.0, boot="boot-a")
    anchor = anchor_at(clock, HOUR)

    clock.advance(30 * 60)
    extended = anchor.extended_by(30 * 60)

    assert extended.ends_at == anchor.ends_at + 30 * 60
    assert reconcile(extended, clock).remaining_seconds == pytest.approx(HOUR)


def test_a_session_cannot_be_anchored_to_a_non_positive_duration() -> None:
    clock = FakeClock(wall_time=START_WALL, boottime=100.0, boot="boot-a")
    with pytest.raises(ValueError):
        anchor_at(clock, 0)


def test_round_trip_through_serialisation_keeps_every_reading() -> None:
    clock = FakeClock(wall_time=START_WALL, boottime=100.0, boot="boot-a")
    anchor = anchor_at(clock, 2 * HOUR)

    assert TimeAnchor.from_dict(anchor.to_dict()) == anchor
