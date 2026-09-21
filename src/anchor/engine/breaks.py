"""Breaks: when they fall, how long they last, and who may avoid them (SPEC 10).

A session alternates between working and resting, and the pattern belongs to
the profile. Everything here is decided in the engine, because a break is a
decision about the session and the session is the engine's (SPEC 5.1).

The phase has a clock of its own, a :class:`TimeAnchor` exactly like the
session's. That is not tidiness: it means a break inherits the reconciliation
SPEC 6.2 already demands of sessions, so suspending the machine, rebooting it
and moving the system clock all behave the same way for a break as they do for
the session containing it, and are tested the same way.

Two rules from SPEC 10 shape most of this:

* **The work timer keeps running during idle and suspend.** Anchor does not
  try to detect whether someone is at the keyboard. A timer that stopped when
  you stood up would be a timer that never fired.
* **If an absence covers a break, the break counts as taken and a new work
  cycle starts.** So eight hours asleep is one missed break rather than nine,
  and nobody comes back from lunch to a queue of overlays.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Self

from anchor.engine.profiles import BreakSettings
from anchor.engine.timekeeping import Clock, TimeAnchor, reconcile
from anchor.protocol.errors import AnchorError, ErrorCode
from anchor.protocol.types import SessionPhase

#: How long a postponement buys. SPEC 10 names five minutes for Moderate, and
#: an unlimited number of postponements for Flexible; there is no reason for
#: the two to be different lengths, so they are not.
POSTPONE_SECONDS: int = 5 * 60

#: One published break event: its name and its payload.
BreakEvent = tuple[str, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a tick did: the state to keep, and what to tell the user."""

    state: BreakState
    events: tuple[BreakEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class BreakState:
    """Where a session is in its work-and-rest pattern."""

    phase: SessionPhase
    anchor: TimeAnchor
    """This phase's own clock: when it started and when it is due to end."""

    cycles_done: int = 0
    """Completed work periods, which is what a long break counts."""

    warned: bool = False
    """Whether the warning for the current work period has been sent."""

    postponed: int = 0
    """Postponements of the break now owed. Reset once it is taken or skipped."""

    long: bool = False
    """Whether the break now running is a long one."""

    taken: int = 0
    postponed_total: int = 0
    skipped: int = 0
    """The three counters SPEC 13 asks statistics to keep."""

    # -- starting --------------------------------------------------------

    @classmethod
    def start(cls, settings: BreakSettings, clock: Clock) -> Self:
        """A session begins working, never resting."""
        return cls(
            phase=SessionPhase.WORKING,
            anchor=TimeAnchor.start(clock, duration_seconds=settings.work_minutes * 60),
        )

    # -- reading ---------------------------------------------------------

    def remaining(self, clock: Clock) -> float:
        """Seconds left of this phase, never negative."""
        return reconcile(self.anchor, clock).remaining_seconds

    @property
    def ends_at(self) -> float:
        """When this phase is due to end, as a wall-clock instant."""
        return self.anchor.ends_at

    @property
    def on_a_break(self) -> bool:
        return self.phase is SessionPhase.BREAK

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": str(self.phase),
            "anchor": self.anchor.to_dict(),
            "cycles_done": self.cycles_done,
            "warned": self.warned,
            "postponed": self.postponed,
            "long": self.long,
            "taken": self.taken,
            "postponed_total": self.postponed_total,
            "skipped": self.skipped,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return cls(
            phase=SessionPhase(raw["phase"]),
            anchor=TimeAnchor.from_dict(raw["anchor"]),
            cycles_done=int(raw.get("cycles_done", 0)),
            warned=bool(raw.get("warned", False)),
            postponed=int(raw.get("postponed", 0)),
            long=bool(raw.get("long", False)),
            taken=int(raw.get("taken", 0)),
            postponed_total=int(raw.get("postponed_total", 0)),
            skipped=int(raw.get("skipped", 0)),
        )


def advance(state: BreakState, settings: BreakSettings, clock: Clock) -> Outcome:
    """Move the pattern on to the current instant.

    Called on every engine tick, and on boot and resume, where the gap since
    the last call can be hours rather than a second.
    """
    outcome = reconcile(state.anchor, clock)
    state = replace(state, anchor=outcome.anchor)

    if not outcome.expired:
        return _maybe_warn(state, settings, clock)

    overshoot = _overshoot(state, clock)
    if state.phase is SessionPhase.BREAK:
        # However long ago it ended, it was taken: the user rested through it,
        # or was away, and either way the break happened.
        return Outcome(
            state=_begin_work(state, settings, clock, taken=True),
            events=(
                (
                    "break.ended",
                    {
                        "taken": state.taken + 1,
                        "cycles_done": state.cycles_done + 1,
                        "long": state.long,
                        "type": str(settings.type),
                    },
                ),
            ),
        )

    if overshoot >= _break_seconds(state, settings, upcoming=True):
        # The work period ended long enough ago that the whole break went by
        # while nobody was there (SPEC 10). It counts, and nothing is said:
        # an overlay for a break that finished an hour ago is a lie with a
        # countdown on it.
        return Outcome(state=_begin_work(state, settings, clock, taken=True))

    return _begin_break(state, settings, clock)


def skip(state: BreakState, settings: BreakSettings, clock: Clock) -> BreakState:
    """Give up the break now running. Flexible profiles only (SPEC 10)."""
    _require_a_break(state, "skip")
    if not settings.hardness.can_skip:
        raise AnchorError(
            f"a {settings.hardness.value} break cannot be skipped",
            code=ErrorCode.SKIP_FORBIDDEN,
        )
    return replace(
        _begin_work(state, settings, clock, taken=False),
        skipped=state.skipped + 1,
    )


def postpone(state: BreakState, settings: BreakSettings, clock: Clock) -> BreakState:
    """Push the break back, if this profile allows it (SPEC 10).

    Postponing is not skipping: the break is still owed, and comes back when
    the borrowed time runs out.
    """
    _require_a_break(state, "postpone")
    limit = settings.hardness.postpone_limit
    if limit is not None and state.postponed >= limit:
        raise AnchorError(
            f"a {settings.hardness.value} break cannot be postponed again",
            code=ErrorCode.SKIP_FORBIDDEN,
        )

    return replace(
        state,
        phase=SessionPhase.WORKING,
        anchor=TimeAnchor.start(clock, duration_seconds=POSTPONE_SECONDS),
        warned=False,
        postponed=state.postponed + 1,
        postponed_total=state.postponed_total + 1,
        long=False,
    )


# -- the parts ----------------------------------------------------------


def _require_a_break(state: BreakState, what: str) -> None:
    if not state.on_a_break:
        raise AnchorError(
            f"there is no break to {what}",
            code=ErrorCode.SKIP_FORBIDDEN,
        )


def _maybe_warn(state: BreakState, settings: BreakSettings, clock: Clock) -> Outcome:
    """Announce the break before it arrives (ADR 2)."""
    if state.phase is not SessionPhase.WORKING or state.warned:
        return Outcome(state=state)
    if settings.warning_seconds <= 0:
        return Outcome(state=state)

    remaining = state.remaining(clock)
    if remaining > settings.warning_seconds:
        return Outcome(state=state)

    return Outcome(
        state=replace(state, warned=True),
        events=(
            (
                "break.warning",
                {
                    "seconds": remaining,
                    "break_seconds": _break_seconds(state, settings, upcoming=True),
                },
            ),
        ),
    )


def _begin_break(state: BreakState, settings: BreakSettings, clock: Clock) -> Outcome:
    seconds = _break_seconds(state, settings, upcoming=True)
    long = _is_long(state, settings)
    started = replace(
        state,
        phase=SessionPhase.BREAK,
        anchor=TimeAnchor.start(clock, duration_seconds=seconds),
        warned=False,
        long=long,
    )
    return Outcome(
        state=started,
        events=(
            (
                "break.started",
                {
                    "seconds": seconds,
                    "long": long,
                    "type": str(settings.type),
                    "hardness": str(settings.hardness),
                    "can_skip": settings.hardness.can_skip,
                    "can_postpone": _can_postpone(started, settings),
                    "allow_sites": settings.allow_sites_during_breaks,
                },
            ),
        ),
    )


def _begin_work(
    state: BreakState, settings: BreakSettings, clock: Clock, *, taken: bool
) -> BreakState:
    """Start a fresh work period, counting the break that just ended."""
    return replace(
        state,
        phase=SessionPhase.WORKING,
        anchor=TimeAnchor.start(clock, duration_seconds=settings.work_minutes * 60),
        cycles_done=state.cycles_done + 1,
        warned=False,
        postponed=0,
        long=False,
        taken=state.taken + 1 if taken else state.taken,
    )


def _can_postpone(state: BreakState, settings: BreakSettings) -> bool:
    limit = settings.hardness.postpone_limit
    return limit is None or state.postponed < limit


def _is_long(state: BreakState, settings: BreakSettings) -> bool:
    """Whether the break about to start is a long one (SPEC 10)."""
    every = settings.long_break_every
    if not every or not settings.long_break_minutes:
        return False
    return (state.cycles_done + 1) % every == 0


def _break_seconds(state: BreakState, settings: BreakSettings, *, upcoming: bool) -> int:
    """How long the break in question lasts."""
    long = _is_long(state, settings) if upcoming else state.long
    if long and settings.long_break_minutes:
        return settings.long_break_minutes * 60
    return settings.break_minutes * 60


def _overshoot(state: BreakState, clock: Clock) -> float:
    """How long ago this phase should have ended."""
    return max(0.0, clock.wall() - state.anchor.ends_at)
