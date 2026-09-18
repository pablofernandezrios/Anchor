"""Profiles: what a session blocks and how it breaks (SPEC 12).

A profile is the reusable part of a session. The level and the valve are not
part of it: those are chosen each time a session starts (SPEC 7.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Self

from anchor.protocol.types import BreakHardness, BreakType, WebMode

#: The shipped break patterns, as (work minutes, break minutes) (SPEC 10).
NAMED_PATTERNS: dict[str, tuple[int, int]] = {
    "25/5": (25, 5),
    "50/10": (50, 10),
    "90/20": (90, 20),
}


@dataclass(frozen=True, slots=True)
class BreakSettings:
    """How breaks behave for one profile (SPEC 10)."""

    work_minutes: int = 50
    break_minutes: int = 10
    type: BreakType = BreakType.OVERLAY
    hardness: BreakHardness = BreakHardness.MODERATE
    long_break_every: int | None = None
    long_break_minutes: int | None = None
    allow_sites_during_breaks: bool = False

    def __post_init__(self) -> None:
        if self.work_minutes <= 0 or self.break_minutes <= 0:
            raise ValueError("break pattern needs positive work and break lengths")
        if self.long_break_every is not None and self.long_break_every <= 0:
            raise ValueError("long_break_every must be positive when set")

    @property
    def pattern_name(self) -> str:
        for name, (work, rest) in NAMED_PATTERNS.items():
            if (work, rest) == (self.work_minutes, self.break_minutes):
                return name
        return "custom"

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_minutes": self.work_minutes,
            "break_minutes": self.break_minutes,
            "type": str(self.type),
            "hardness": str(self.hardness),
            "long_break_every": self.long_break_every,
            "long_break_minutes": self.long_break_minutes,
            "allow_sites_during_breaks": self.allow_sites_during_breaks,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return cls(
            work_minutes=int(raw.get("work_minutes", 50)),
            break_minutes=int(raw.get("break_minutes", 10)),
            type=BreakType(raw.get("type", BreakType.OVERLAY)),
            hardness=BreakHardness(raw.get("hardness", BreakHardness.MODERATE)),
            long_break_every=raw.get("long_break_every"),
            long_break_minutes=raw.get("long_break_minutes"),
            allow_sites_during_breaks=bool(raw.get("allow_sites_during_breaks", False)),
        )


@dataclass(frozen=True, slots=True)
class Profile:
    """A named set of web rules, applications and break settings (SPEC 12)."""

    name: str
    web_mode: WebMode = WebMode.BLOCKLIST
    categories: frozenset[str] = frozenset()
    domains: frozenset[str] = frozenset()
    apps: frozenset[str] = frozenset()
    breaks: BreakSettings = field(default_factory=BreakSettings)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a profile needs a name")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "web_mode": str(self.web_mode),
            "categories": sorted(self.categories),
            "domains": sorted(self.domains),
            "apps": sorted(self.apps),
            "breaks": self.breaks.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        return cls(
            name=str(raw["name"]),
            web_mode=WebMode(raw.get("web_mode", WebMode.BLOCKLIST)),
            categories=frozenset(raw.get("categories", ())),
            domains=frozenset(raw.get("domains", ())),
            apps=frozenset(raw.get("apps", ())),
            breaks=BreakSettings.from_dict(raw.get("breaks", {})),
        )
