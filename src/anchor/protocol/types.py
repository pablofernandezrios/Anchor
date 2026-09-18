"""Shared vocabulary for every Anchor component (SPEC 4).

These enumerations are part of the wire protocol, so their values are stable:
they appear in IPC messages, in ``config.json`` and ``state.json``, and in the
statistics database. Renaming a value is a breaking change.
"""

from __future__ import annotations

from enum import StrEnum


class Level(StrEnum):
    """How hard a session is to leave (SPEC 7.2).

    The declaration order is the strictness order, which the ratchet (SPEC 7.4)
    and schedule merging (SPEC 11) both rely on.
    """

    SOFT = "soft"
    FIRM = "firm"
    STRICT = "strict"

    @property
    def strictness(self) -> int:
        return _LEVEL_ORDER[self]

    def is_at_least(self, other: Level) -> bool:
        return self.strictness >= other.strictness

    @classmethod
    def strictest(cls, levels: object) -> Level:
        """Return the strictest of ``levels``.

        Used when overlapping schedules are merged: the strictest level wins
        (SPEC 11).
        """
        if not isinstance(levels, list | tuple | set | frozenset):
            raise TypeError("levels must be a collection of Level values")
        chosen: Level | None = None
        for level in levels:
            if not isinstance(level, Level):
                raise TypeError(f"not a Level: {level!r}")
            if chosen is None or level.strictness > chosen.strictness:
                chosen = level
        if chosen is None:
            raise ValueError("cannot take the strictest of no levels")
        return chosen


_LEVEL_ORDER: dict[Level, int] = {Level.SOFT: 0, Level.FIRM: 1, Level.STRICT: 2}


class Valve(StrEnum):
    """The emergency exit from a Strict session, fixed at start (SPEC 7.5)."""

    WAIT = "wait"
    PHRASE = "phrase"
    BOTH = "both"

    @property
    def needs_wait(self) -> bool:
        return self in (Valve.WAIT, Valve.BOTH)

    @property
    def needs_phrase(self) -> bool:
        return self in (Valve.PHRASE, Valve.BOTH)


class WebMode(StrEnum):
    """Whether a profile blocks what is listed, or everything else (SPEC 8.1)."""

    BLOCKLIST = "blocklist"
    ALLOWLIST = "allowlist"


class BreakType(StrEnum):
    """How a break announces itself (SPEC 10)."""

    NOTIFICATION = "notification"
    OVERLAY = "overlay"


class BreakHardness(StrEnum):
    """How hard a break is to avoid (SPEC 10). Independent of :class:`Level`."""

    FLEXIBLE = "flexible"
    MODERATE = "moderate"
    MANDATORY = "mandatory"

    @property
    def can_skip(self) -> bool:
        return self is BreakHardness.FLEXIBLE

    @property
    def postpone_limit(self) -> int | None:
        """Number of postponements allowed, or ``None`` for unlimited."""
        match self:
            case BreakHardness.FLEXIBLE:
                return None
            case BreakHardness.MODERATE:
                return 1
            case BreakHardness.MANDATORY:
                return 0


class SessionOrigin(StrEnum):
    """Whether the user started the session or a schedule did (SPEC 7.1, 11)."""

    MANUAL = "manual"
    SCHEDULE = "schedule"


class RuptureKind(StrEnum):
    """Escapes and tampering, all of which are recorded (SPEC 4, 13)."""

    VALVE = "valve"
    SKIP = "skip"
    TAMPERING = "tampering"


class SessionPhase(StrEnum):
    """What the current session is doing right now."""

    IDLE = "idle"
    WORKING = "working"
    BREAK = "break"
