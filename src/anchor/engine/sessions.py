"""Sessions: starting them, extending them, and paying to leave (SPEC 7).

A session is immutable. Every transition returns a new :class:`Session`, so the
engine can validate a change, persist it, and only then swap it in; a refused
change cannot leave a half-applied session behind.

Leaving early always costs the same two things in different amounts: waiting,
and typing. Soft asks for a short wait. Firm asks for a long wait and a long
phrase. Strict does not let go at all, and only the valve chosen at the start
applies (SPEC 7.2, 7.5).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Final, Self

from anchor.engine.phrases import DEFAULT_PHRASE_LENGTH, generate_phrase, phrase_matches
from anchor.engine.timekeeping import Clock, TimeAnchor
from anchor.protocol.errors import AnchorError, ErrorCode
from anchor.protocol.messages import MAX_MANUAL_DURATION_SECONDS
from anchor.protocol.types import (
    Level,
    RuptureKind,
    SessionOrigin,
    SessionPhase,
    Valve,
)

#: Time a Soft session makes you wait before it lets go (SPEC 7.2).
SOFT_CANCEL_WAIT_SECONDS: Final = 5 * 60

#: Default Firm wait, configurable but never during a session (SPEC 7.2).
DEFAULT_FIRM_WAIT_SECONDS: Final = 15 * 60

#: The wait built into the valve (SPEC 7.5).
VALVE_WAIT_SECONDS: Final = 30 * 60

#: How long the applications already open have to save their work when a
#: session starts (SPEC 7.1). It belongs to the session rather than to the
#: blocker, so that restarting the blocker cannot hand out a fresh two minutes.
GRACE_SECONDS: Final = 2 * 60


class ExitKind(StrEnum):
    """Why someone is trying to end a session early."""

    CANCEL = "cancel"
    """Soft and Firm only (SPEC 7.2)."""

    VALVE = "valve"
    """Strict only. Always a rupture (SPEC 7.5)."""


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    """The knobs SPEC 7.2 allows the user to set, outside a session."""

    soft_wait_seconds: int = SOFT_CANCEL_WAIT_SECONDS
    firm_wait_seconds: int = DEFAULT_FIRM_WAIT_SECONDS
    valve_wait_seconds: int = VALVE_WAIT_SECONDS
    phrase_length: int = DEFAULT_PHRASE_LENGTH


@dataclass(frozen=True, slots=True)
class Rupture:
    """An escape or a tampering event. Always recorded (SPEC 4, 13)."""

    kind: RuptureKind
    at: float
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": str(self.kind), "at": self.at, "detail": self.detail}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return cls(
            kind=RuptureKind(raw["kind"]),
            at=float(raw["at"]),
            detail=str(raw.get("detail", "")),
        )


@dataclass(frozen=True, slots=True)
class ExitRequest:
    """A pending attempt to leave, and what it still costs."""

    kind: ExitKind
    requested_at: float
    requested_at_boottime: float
    boot_id: str
    wait_seconds: int
    phrase: str | None

    def elapsed(self, clock: Clock) -> float:
        """Seconds since the request, measured against the boot clock.

        Using the boot clock means winding the wall clock forward does not
        satisfy the wait (SPEC 6.2). After a reboot the boot clock has restarted
        and only the wall clock remains, which SPEC 16 records as a known gap.
        """
        if self.boot_id and clock.boot_id() == self.boot_id:
            return clock.boottime() - self.requested_at_boottime
        return clock.wall() - self.requested_at

    def remaining_wait(self, clock: Clock) -> float:
        return max(0.0, self.wait_seconds - self.elapsed(clock))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "requested_at": self.requested_at,
            "requested_at_boottime": self.requested_at_boottime,
            "boot_id": self.boot_id,
            "wait_seconds": self.wait_seconds,
            "phrase": self.phrase,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return cls(
            kind=ExitKind(raw["kind"]),
            requested_at=float(raw["requested_at"]),
            requested_at_boottime=float(raw["requested_at_boottime"]),
            boot_id=str(raw.get("boot_id", "")),
            wait_seconds=int(raw["wait_seconds"]),
            phrase=raw.get("phrase"),
        )


@dataclass(frozen=True, slots=True)
class ExitOutcome:
    """The result of completing an exit."""

    ended: bool
    rupture: Rupture | None = None


@dataclass(frozen=True, slots=True)
class Session:
    """A period during which a profile's blocks are active (SPEC 4)."""

    id: str
    profile: str
    level: Level
    origin: SessionOrigin
    anchor: TimeAnchor
    valve: Valve | None = None
    phase: SessionPhase = SessionPhase.WORKING
    exit_request: ExitRequest | None = None
    blocked_attempts: int = 0
    app_blocks: int = 0
    """Applications closed because this session blocks them (SPEC 13)."""

    ruptures: tuple[Rupture, ...] = field(default_factory=tuple)

    # -- time -----------------------------------------------------------

    def extended_by(self, seconds: float) -> Session:
        """Return this session, lengthened (SPEC 7.3).

        There is no upper bound here: the 8-hour cap applies to the duration
        chosen at start, not to what a session may grow into.
        """
        if seconds <= 0:
            raise AnchorError(
                "an extension must be a positive amount of time",
                code=ErrorCode.INVALID_DURATION,
            )
        return replace(self, anchor=self.anchor.extended_by(seconds))

    # -- leaving --------------------------------------------------------

    def request_exit(self, kind: ExitKind, *, clock: Clock, policy: SessionPolicy) -> Session:
        """Start the wait, and generate the phrase if one is owed."""
        wait_seconds, needs_phrase = self._exit_cost(kind, policy)
        request = ExitRequest(
            kind=kind,
            requested_at=clock.wall(),
            requested_at_boottime=clock.boottime(),
            boot_id=clock.boot_id(),
            wait_seconds=wait_seconds,
            phrase=generate_phrase(policy.phrase_length) if needs_phrase else None,
        )
        return replace(self, exit_request=request)

    def _exit_cost(self, kind: ExitKind, policy: SessionPolicy) -> tuple[int, bool]:
        """Return the wait and whether a phrase is required (SPEC 7.2, 7.5)."""
        if kind is ExitKind.CANCEL:
            match self.level:
                case Level.SOFT:
                    return policy.soft_wait_seconds, False
                case Level.FIRM:
                    return policy.firm_wait_seconds, True
                case Level.STRICT:
                    raise AnchorError(
                        "a Strict session cannot be cancelled; use the emergency valve",
                        code=ErrorCode.CANCEL_FORBIDDEN,
                    )

        if self.level is not Level.STRICT or self.valve is None:
            raise AnchorError(
                "the emergency valve exists only in Strict sessions",
                code=ErrorCode.BAD_REQUEST,
            )
        return (
            policy.valve_wait_seconds if self.valve.needs_wait else 0,
            self.valve.needs_phrase,
        )

    def withdraw_exit(self) -> Session:
        """Change your mind (SPEC 7.5). The wait starts again next time."""
        return replace(self, exit_request=None)

    def complete_exit(self, *, clock: Clock, typed: str | None = None) -> ExitOutcome:
        """Finish leaving, if the wait has passed and the phrase is right."""
        request = self.exit_request
        if request is None:
            raise AnchorError(
                "nothing to complete: no exit has been requested",
                code=ErrorCode.VALVE_NOT_REQUESTED,
            )

        remaining = request.remaining_wait(clock)
        if remaining > 0:
            minutes = remaining / 60
            raise AnchorError(
                f"{minutes:.0f} minute(s) of the wait are left",
                code=ErrorCode.WAIT_NOT_ELAPSED,
            )

        if request.phrase is not None and not phrase_matches(request.phrase, typed):
            raise AnchorError(
                "the phrase does not match the one Anchor generated",
                code=ErrorCode.PHRASE_MISMATCH,
            )

        rupture = (
            Rupture(
                kind=RuptureKind.VALVE,
                at=clock.wall(),
                detail=f"valve {self.valve} used on profile {self.profile}",
            )
            if request.kind is ExitKind.VALVE
            else None
        )
        return ExitOutcome(ended=True, rupture=rupture)

    # -- bookkeeping ----------------------------------------------------

    def with_rupture(self, rupture: Rupture) -> Session:
        return replace(self, ruptures=(*self.ruptures, rupture))

    def with_blocked_attempt(self) -> Session:
        return replace(self, blocked_attempts=self.blocked_attempts + 1)

    def with_app_blocks(self, count: int) -> Session:
        """Count applications this session closed (SPEC 13).

        Kept apart from ``blocked_attempts`` because SPEC 13 asks for blocked
        attempts by domain *and* by application, and a single number could
        answer neither question.
        """
        if count < 0:
            raise ValueError("cannot record a negative number of applications")
        return replace(self, app_blocks=self.app_blocks + count)

    def in_phase(self, phase: SessionPhase) -> Session:
        return replace(self, phase=phase)

    # -- persistence ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "profile": self.profile,
            "level": str(self.level),
            "origin": str(self.origin),
            "anchor": self.anchor.to_dict(),
            "valve": str(self.valve) if self.valve else None,
            "phase": str(self.phase),
            "exit_request": self.exit_request.to_dict() if self.exit_request else None,
            "blocked_attempts": self.blocked_attempts,
            "app_blocks": self.app_blocks,
            "ruptures": [rupture.to_dict() for rupture in self.ruptures],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        exit_raw = raw.get("exit_request")
        return cls(
            id=str(raw["id"]),
            profile=str(raw["profile"]),
            level=Level(raw["level"]),
            origin=SessionOrigin(raw.get("origin", SessionOrigin.MANUAL)),
            anchor=TimeAnchor.from_dict(raw["anchor"]),
            valve=Valve(raw["valve"]) if raw.get("valve") else None,
            phase=SessionPhase(raw.get("phase", SessionPhase.WORKING)),
            exit_request=ExitRequest.from_dict(exit_raw) if exit_raw else None,
            blocked_attempts=int(raw.get("blocked_attempts", 0)),
            app_blocks=int(raw.get("app_blocks", 0)),
            ruptures=tuple(Rupture.from_dict(item) for item in raw.get("ruptures", ())),
        )


def start_session(
    *,
    profile: str,
    level: Level,
    duration_seconds: float,
    valve: Valve | None,
    origin: SessionOrigin,
    clock: Clock,
    policy: SessionPolicy,
) -> Session:
    """Validate and create a session (SPEC 7.1).

    ``policy`` is accepted so that starting a session goes through the same
    settings object as leaving one, even though nothing here reads it yet.
    """
    del policy  # Reserved: start-time policy checks arrive with schedules.

    if duration_seconds <= 0:
        raise AnchorError("a session needs a positive duration", code=ErrorCode.INVALID_DURATION)

    if origin is SessionOrigin.MANUAL and duration_seconds > MAX_MANUAL_DURATION_SECONDS:
        hours = MAX_MANUAL_DURATION_SECONDS / 3600
        raise AnchorError(
            f"a session started by hand can last at most {hours:.0f} hours; "
            "extend it later if you need more",
            code=ErrorCode.DURATION_TOO_LONG,
        )

    if level is Level.STRICT and valve is None:
        raise AnchorError(
            "a Strict session needs an emergency valve, because it cannot be cancelled",
            code=ErrorCode.BAD_REQUEST,
        )
    if level is not Level.STRICT and valve is not None:
        raise AnchorError(
            f"the emergency valve applies to Strict sessions only, not {level}",
            code=ErrorCode.BAD_REQUEST,
        )

    return Session(
        id=str(uuid.uuid4()),
        profile=profile,
        level=level,
        origin=origin,
        anchor=TimeAnchor.start(clock, duration_seconds=duration_seconds),
        valve=valve,
    )
