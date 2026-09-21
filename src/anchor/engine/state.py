"""The engine's persistent state (SPEC 6.1).

Holds what must survive a restart: the active session, the skips spent this
week, and any pending request to leave. The engine is the only writer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Self

from anchor.engine.sessions import Session

#: Skips allowed per week for Soft and Firm scheduled sessions (SPEC 11).
SKIPS_PER_WEEK = 3


@dataclass(slots=True)
class EngineState:
    """Everything the engine remembers between restarts."""

    session: Session | None = None
    skips_used: int = 0
    skips_week_start: str = ""
    """ISO date of the Monday the skip count belongs to (SPEC 11)."""

    ruptures: list[dict[str, Any]] = field(default_factory=list)
    """Ruptures recorded since the last statistics flush."""

    skipped: dict[str, float] = field(default_factory=dict)
    """Schedule occurrences the user skipped, by identifier (SPEC 11).

    The value is when that occurrence ends. Without this the next tick would
    start the session again a second after it was skipped, which is not what
    anyone means by skipping.
    """

    @property
    def skips_remaining(self) -> int:
        return max(0, SKIPS_PER_WEEK - self.skips_used)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session.to_dict() if self.session else None,
            "skips_used": self.skips_used,
            "skips_week_start": self.skips_week_start,
            "ruptures": list(self.ruptures),
            "skipped": dict(self.skipped),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        session_raw = raw.get("session")
        return cls(
            session=Session.from_dict(session_raw) if session_raw else None,
            skips_used=int(raw.get("skips_used", 0)),
            skips_week_start=str(raw.get("skips_week_start", "")),
            ruptures=list(raw.get("ruptures", [])),
            skipped={str(key): float(value) for key, value in (raw.get("skipped") or {}).items()},
        )
